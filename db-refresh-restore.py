#!/usr/bin/env python3
"""
Snapshot prod, copy it into dev, restore it to a throwaway instance,
anonymize it (modules/anonymize's ECS task), then rename-swap it into place
as the live dev instance (old live -> "-previous", throwaway -> live) and
repoint Terraform's tracked resource at it via `state rm` + `import` (never
a plain `apply` against the stale state -- that would just rename the old
instance back, since Terraform still tracks it by its immutable
DbiResourceId regardless of what it's currently named).

Locally: needs an active `aws sso login` (or any ambient credentials) that's
allowed to assume AdministratorAccess in both accounts directly.
In CI: needs $GITLAB_OIDC_TOKEN plus $SOURCE_ACCOUNT_ROLE_ARN/$TARGET_ACCOUNT_ROLE_ARN
(each account's own role trusted by GitLab's OIDC provider) -- see
assume_admin(). Either way, also needs `terraform` on $PATH and your normal
dev backend credentials (same as running `terraform apply` in
environments/dev by hand).

Afterwards, clean up by hand once you're done with the copied snapshot:
    aws rds delete-db-snapshot --region eu-west-1 \\
        --db-snapshot-identifier testdbmigration-refresh-cp
"""

import json
import os
import subprocess
import time

import boto3

PROJECT = os.environ.get("PROJECT", "testdbmigration")

PROD_ACCOUNT_ID = os.environ.get("PROD_ACCOUNT_ID", "230562640235")
PROD_REGION = os.environ.get("PROD_REGION", "eu-central-1")
PROD_DB_INSTANCE_IDENTIFIER = os.environ.get("PROD_DB_INSTANCE_IDENTIFIER", f"{PROJECT}-prod-database")

DEV_ACCOUNT_ID = os.environ.get("DEV_ACCOUNT_ID", "246402711611")
DEV_REGION = os.environ.get("DEV_REGION", "eu-west-1")
DEV_ENVIRONMENT = os.environ.get("DEV_ENVIRONMENT", "dev")
DEV_INSTANCE_CLASS = os.environ.get("DEV_INSTANCE_CLASS", "db.t4g.micro")

# Naming follows modules/anonymize (module.anonymize in environments/dev):
# cluster_name/task_definition_family are both "${project}-anonymize".
ECS_CLUSTER_NAME = os.environ.get("ECS_CLUSTER_NAME", f"{PROJECT}-anonymize")
ECS_TASK_DEFINITION_FAMILY = os.environ.get("ECS_TASK_DEFINITION_FAMILY", f"{PROJECT}-anonymize")
ECS_CONTAINER_NAME = os.environ.get("ECS_CONTAINER_NAME", "psql")

# Only set in CI: prod and dev each have their own bootstrap role trusted by
# GitLab's OIDC provider (no single ambient identity reaches both, unlike
# `aws sso login` locally), so each side hops through its own on the way to
# that account's AdministratorAccess. See assume_admin().
SOURCE_ACCOUNT_ROLE_ARN = os.environ.get("SOURCE_ACCOUNT_ROLE_ARN")
TARGET_ACCOUNT_ROLE_ARN = os.environ.get("TARGET_ACCOUNT_ROLE_ARN")
GITLAB_OIDC_TOKEN = os.environ.get("GITLAB_OIDC_TOKEN")

SRC_SNAPSHOT_ID = f"{PROJECT}-refresh-src"
COPY_SNAPSHOT_ID = f"{PROJECT}-refresh-cp"
TMP_INSTANCE_ID = f"{PROJECT}-refresh-tmp"

POLL_INTERVAL = 30

# environments/dev, relative to this script's own location so it works no
# matter what directory you run it from.
TF_DEV_ROOT = os.environ.get(
    "TF_DEV_ROOT",
    os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "environments", "dev")),
)
TF_RESOURCE_ADDRESS = "module.app.module.rds-main.aws_db_instance.default[0]"


def log(message):
    print(f"[manual-refresh] {message}", flush=True)


def assume_admin(account_id, region, bootstrap_role_arn=None):
    """Assume AdministratorAccess in the given account. Returns
    (boto3.Session, bootstrap_env) -- bootstrap_env is a dict of
    AWS_ACCESS_KEY_ID/SECRET/SESSION_TOKEN for the *bootstrap* hop (None
    locally), needed later to hand to the `terraform` subprocess: it reads
    AWS credentials from the process environment, not from a boto3.Session
    object in this Python process, so terraform's own S3-backend/provider
    assume-role into AdministratorAccess needs the same ambient credentials
    -- either your local `aws sso login` session, or in CI, the bootstrap
    role's session.

    Locally, ambient credentials from `aws sso login` already reach both
    accounts' AdministratorAccess directly -- one hop. In CI, bootstrap_role_arn
    is set (a per-account role trusted by GitLab's OIDC provider), so this
    hops through it first via AssumeRoleWithWebIdentity, then assumes
    AdministratorAccess from there -- same double-hop terraform's own
    provider blocks already do for apply-dev/apply-prod.
    """
    bootstrap_env = None
    if bootstrap_role_arn:
        if not GITLAB_OIDC_TOKEN:
            raise SystemExit("SOURCE_ACCOUNT_ROLE_ARN/TARGET_ACCOUNT_ROLE_ARN is set but GITLAB_OIDC_TOKEN isn't")
        log(f"assuming bootstrap role {bootstrap_role_arn} via OIDC")
        bootstrap_sts = boto3.client("sts", region_name=region)
        bootstrap_creds = bootstrap_sts.assume_role_with_web_identity(
            RoleArn=bootstrap_role_arn,
            RoleSessionName="db-refresh-ci-bootstrap",
            WebIdentityToken=GITLAB_OIDC_TOKEN,
        )["Credentials"]
        bootstrap_env = {
            "AWS_ACCESS_KEY_ID": bootstrap_creds["AccessKeyId"],
            "AWS_SECRET_ACCESS_KEY": bootstrap_creds["SecretAccessKey"],
            "AWS_SESSION_TOKEN": bootstrap_creds["SessionToken"],
        }
        sts = boto3.client("sts", region_name=region, **{
            "aws_access_key_id": bootstrap_creds["AccessKeyId"],
            "aws_secret_access_key": bootstrap_creds["SecretAccessKey"],
            "aws_session_token": bootstrap_creds["SessionToken"],
        })
    else:
        sts = boto3.client("sts", region_name=region)

    log(f"assuming AdministratorAccess in account {account_id}")
    creds = sts.assume_role(
        RoleArn=f"arn:aws:iam::{account_id}:role/AdministratorAccess",
        RoleSessionName="db-refresh-manual",
    )["Credentials"]
    session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )
    return session, bootstrap_env


def poll_until(what, fn, done, failed=lambda status: False, interval=POLL_INTERVAL, timeout=7200):
    start = time.time()
    while True:
        status = fn()
        if failed(status):
            raise RuntimeError(f"{what} failed: {status}")
        if done(status):
            return status
        if time.time() - start > timeout:
            raise RuntimeError(f"{what} timed out after {timeout}s; last status: {status}")
        log(f"{what}: {status} -- waiting {interval}s")
        time.sleep(interval)


def describe_snapshot(rds, snapshot_id):
    try:
        snaps = rds.describe_db_snapshots(DBSnapshotIdentifier=snapshot_id)["DBSnapshots"]
    except rds.exceptions.DBSnapshotNotFoundFault:
        return {"status": "not_found"}
    if not snaps:
        return {"status": "not_found"}
    return {"status": snaps[0]["Status"], "arn": snaps[0]["DBSnapshotArn"]}


def describe_instance(rds, instance_id):
    try:
        insts = rds.describe_db_instances(DBInstanceIdentifier=instance_id)["DBInstances"]
    except rds.exceptions.DBInstanceNotFoundFault:
        return {"status": "not_found"}
    if not insts:
        return {"status": "not_found"}
    inst = insts[0]
    endpoint = inst.get("Endpoint") or {}
    return {"status": inst["DBInstanceStatus"], "address": endpoint.get("Address"), "port": endpoint.get("Port")}


def rename_instance(rds, instance_id, new_instance_id):
    rds.modify_db_instance(
        DBInstanceIdentifier=instance_id, NewDBInstanceIdentifier=new_instance_id, ApplyImmediately=True
    )


def discover_dev_network(dev_session):
    """The cloudposse RDS module creates its own security group and subnet
    group named after the instance itself: "<project>-<environment>-database"
    -- confirmed against the real dev account rather than assumed.
    """
    name = f"{PROJECT}-{DEV_ENVIRONMENT}-database"

    ec2 = dev_session.client("ec2", region_name=DEV_REGION)
    sgs = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [name]}])["SecurityGroups"]
    if not sgs:
        raise RuntimeError(f"no security group named {name} found in dev -- pass DEV_SECURITY_GROUP_ID by hand")
    security_group_id = sgs[0]["GroupId"]

    # The RDS security group only allows inbound 5432 *from* the VPC's main
    # security group (confirmed against the real ingress rule) -- not from
    # itself. Anything that needs to actually reach RDS (the anonymize ECS
    # task, same as the db-loop EC2 instance) has to be a member of *this*
    # one, not security_group_id above.
    main_name = f"{PROJECT}-{DEV_ENVIRONMENT}-main"
    main_sgs = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [main_name]}])["SecurityGroups"]
    if not main_sgs:
        raise RuntimeError(f"no security group named {main_name} found in dev -- pass DEV_MAIN_SECURITY_GROUP_ID by hand")
    vpc_main_security_group_id = main_sgs[0]["GroupId"]

    kms = dev_session.client("kms", region_name=DEV_REGION)
    kms_key_arn = kms.describe_key(KeyId=f"alias/{PROJECT}-{DEV_ENVIRONMENT}-rds")["KeyMetadata"]["Arn"]

    # Dev's VPC has no NAT gateway, so the anonymize ECS task runs in a
    # public subnet (same reasoning as the db-loop EC2 instance).
    subnets = ec2.describe_subnets(Filters=[{"Name": f"tag:{PROJECT}/subnet/type", "Values": ["public"]}])["Subnets"]
    if not subnets:
        raise RuntimeError(f"no public subnets tagged {PROJECT}/subnet/type=public found in dev")
    public_subnet_ids = [s["SubnetId"] for s in subnets]

    return {
        "security_group_id": security_group_id,
        "vpc_main_security_group_id": vpc_main_security_group_id,
        "kms_key_arn": kms_key_arn,
        "db_subnet_group_name": name,
        "public_subnet_ids": public_subnet_ids,
    }


def run_ecs_task(ecs, cluster, task_definition, container_name, subnet_ids, security_group_id, db_host, db_port):
    resp = ecs.run_task(
        cluster=cluster,
        taskDefinition=task_definition,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": subnet_ids,
                "securityGroups": [security_group_id],
                "assignPublicIp": "ENABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": container_name,
                    "environment": [
                        {"name": "DB_HOST", "value": db_host},
                        {"name": "DB_PORT", "value": str(db_port)},
                    ],
                }
            ]
        },
    )
    failures = resp.get("failures", [])
    if failures:
        raise RuntimeError(f"ecs run_task failed to start: {failures}")
    return resp["tasks"][0]["taskArn"]


def describe_ecs_task(ecs, cluster, task_arn):
    resp = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])
    tasks = resp["tasks"]
    if not tasks:
        return {"status": "not_found"}
    task = tasks[0]
    containers = task.get("containers", [])
    exit_code = containers[0].get("exitCode") if containers else None
    return {"status": task["lastStatus"], "exit_code": exit_code}


def run_anonymize_task(dev_session, dev_network, db_host, db_port):
    log(f"running anonymize task ({ECS_TASK_DEFINITION_FAMILY}) against {db_host}:{db_port}")
    ecs = dev_session.client("ecs", region_name=DEV_REGION)
    task_arn = run_ecs_task(
        ecs, ECS_CLUSTER_NAME, ECS_TASK_DEFINITION_FAMILY, ECS_CONTAINER_NAME,
        dev_network["public_subnet_ids"], dev_network["vpc_main_security_group_id"],
        db_host, db_port,
    )
    result = poll_until(
        "anonymize task",
        lambda: describe_ecs_task(ecs, ECS_CLUSTER_NAME, task_arn),
        done=lambda status: status["status"] == "STOPPED",
        interval=20,
        timeout=1800,
    )
    if result["exit_code"] != 0:
        raise RuntimeError(f"anonymize task exited non-zero: {result}")
    log("anonymize task finished successfully")


def reconcile_terraform_state(live_identifier, bootstrap_env=None):
    """Repoint Terraform's tracked dev DB resource at the instance that just
    got renamed into the live slot.

    Never just `terraform apply` here: Terraform still tracks the OLD
    instance by its immutable DbiResourceId, which doesn't change when you
    rename an RDS instance -- so a plain apply would see identifier="...
    -previous" in AWS, "..." (no suffix) in config, and "fix" it by renaming
    the OLD instance back, undoing the swap. `state rm` drops that stale
    tracking first; `import` picks up the newly-promoted instance under the
    same resource address instead.
    """
    env = {**os.environ, **(bootstrap_env or {})}

    def run_tf(*args, check=True):
        log("terraform " + " ".join(args))
        return subprocess.run(["terraform", *args], cwd=TF_DEV_ROOT, env=env, check=check)

    log(f"reconciling terraform state (cwd={TF_DEV_ROOT})")
    run_tf("init")

    rm = run_tf("state", "rm", TF_RESOURCE_ADDRESS, check=False)
    if rm.returncode != 0:
        log("terraform state rm: nothing to remove (resource not currently tracked) -- continuing")

    imp = run_tf("import", TF_RESOURCE_ADDRESS, live_identifier, check=False)
    if imp.returncode != 0:
        show = subprocess.run(
            ["terraform", "state", "show", TF_RESOURCE_ADDRESS],
            cwd=TF_DEV_ROOT, env=env, capture_output=True, text=True,
        )
        if show.returncode == 0 and live_identifier in show.stdout:
            log("terraform import: resource is already tracked and points at the live identifier -- continuing")
        else:
            raise RuntimeError(
                "terraform import failed and the resource isn't already correctly tracked -- "
                "manual intervention required"
            )

    run_tf("plan", "-out=plan.tfplan")
    plan = json.loads(
        subprocess.run(
            ["terraform", "show", "-json", "plan.tfplan"],
            cwd=TF_DEV_ROOT, env=env, check=True, capture_output=True, text=True,
        ).stdout
    )
    destructive = [
        (rc["address"], rc["change"]["actions"])
        for rc in plan.get("resource_changes", [])
        if "delete" in rc["change"]["actions"]
    ]
    if destructive:
        details = ", ".join(f"{address} ({actions})" for address, actions in destructive)
        raise RuntimeError(
            f"terraform plan shows destroy/replace after import -- refusing to apply: {details}"
        )

    run_tf("apply", "plan.tfplan")
    log("terraform state reconciled -- dev instance is correctly tracked again")


def reset_dev_master_password(dev_session, instance_id):
    """Restoring from the prod snapshot brings prod's master password along
    with it -- reset it back to dev's own password. Called on the throwaway
    instance right after restore (the anonymize task connects with dev's
    password, from the same SSM parameter, so this has to happen before
    that runs, not just before the swap).
    """
    ssm_name = f"/{PROJECT}/{DEV_ENVIRONMENT}/rds-main/master-password"

    log(f"resetting {instance_id}'s master password back to dev's own (SSM parameter {ssm_name})")
    ssm = dev_session.client("ssm", region_name=DEV_REGION)
    password = ssm.get_parameter(Name=ssm_name, WithDecryption=True)["Parameter"]["Value"]

    rds = dev_session.client("rds", region_name=DEV_REGION)
    rds.modify_db_instance(DBInstanceIdentifier=instance_id, MasterUserPassword=password, ApplyImmediately=True)
    poll_until(
        "password reset",
        lambda: describe_instance(rds, instance_id),
        done=lambda status: status["status"] == "available",
        failed=lambda status: status["status"] == "failed",
    )
    log(f"{instance_id}'s master password now matches dev's SSM parameter")


def main():
    live_identifier = f"{PROJECT}-{DEV_ENVIRONMENT}-database"
    previous_identifier = f"{live_identifier}-previous"

    prod, _ = assume_admin(PROD_ACCOUNT_ID, PROD_REGION, bootstrap_role_arn=SOURCE_ACCOUNT_ROLE_ARN)
    dev, dev_bootstrap_env = assume_admin(DEV_ACCOUNT_ID, DEV_REGION, bootstrap_role_arn=TARGET_ACCOUNT_ROLE_ARN)

    prod_rds = prod.client("rds", region_name=PROD_REGION)
    dev_rds = dev.client("rds", region_name=DEV_REGION)

    dev_network = discover_dev_network(dev)
    log(f"dev network: {dev_network}")

    log("preflight: checking for leftovers from a previously-failed run")
    for rds, snapshot_id in ((prod_rds, SRC_SNAPSHOT_ID), (dev_rds, COPY_SNAPSHOT_ID)):
        existing_snapshot = describe_snapshot(rds, snapshot_id)
        if existing_snapshot["status"] != "not_found":
            log(f"preflight: leftover snapshot {snapshot_id} (status={existing_snapshot['status']}) -- deleting it")
            rds.delete_db_snapshot(DBSnapshotIdentifier=snapshot_id)
            poll_until(
                f"deleting leftover {snapshot_id}",
                lambda rds=rds, snapshot_id=snapshot_id: describe_snapshot(rds, snapshot_id)["status"],
                done=lambda status: status == "not_found",
                interval=5,
            )

    existing_tmp = describe_instance(dev_rds, TMP_INSTANCE_ID)
    if existing_tmp["status"] != "not_found":
        log(f"preflight: leftover {TMP_INSTANCE_ID} (status={existing_tmp['status']}) -- deleting it")
        dev_rds.delete_db_instance(DBInstanceIdentifier=TMP_INSTANCE_ID, SkipFinalSnapshot=True)
        poll_until(
            f"deleting leftover {TMP_INSTANCE_ID}",
            lambda: describe_instance(dev_rds, TMP_INSTANCE_ID)["status"],
            done=lambda status: status == "not_found",
        )

    existing_previous = describe_instance(dev_rds, previous_identifier)
    if existing_previous["status"] != "not_found":
        log(f"preflight: leftover {previous_identifier} (status={existing_previous['status']}) from a previous "
            "run's swap -- deleting it so this run's rename has room")
        dev_rds.delete_db_instance(DBInstanceIdentifier=previous_identifier, SkipFinalSnapshot=True)
        poll_until(
            f"deleting leftover {previous_identifier}",
            lambda: describe_instance(dev_rds, previous_identifier)["status"],
            done=lambda status: status == "not_found",
        )

    log(f"creating snapshot of {PROD_DB_INSTANCE_IDENTIFIER}")
    prod_rds.create_db_snapshot(DBInstanceIdentifier=PROD_DB_INSTANCE_IDENTIFIER, DBSnapshotIdentifier=SRC_SNAPSHOT_ID)
    source_arn = poll_until(
        "source snapshot",
        lambda: describe_snapshot(prod_rds, SRC_SNAPSHOT_ID),
        done=lambda status: status["status"] == "available",
        failed=lambda status: status["status"] == "failed",
    )["arn"]

    log(f"sharing snapshot with dev account {DEV_ACCOUNT_ID}")
    prod_rds.modify_db_snapshot_attribute(
        DBSnapshotIdentifier=SRC_SNAPSHOT_ID, AttributeName="restore", ValuesToAdd=[DEV_ACCOUNT_ID]
    )

    log("copying snapshot into dev (cross-account, cross-region, re-encrypted with dev's KMS key)")
    dev_rds.copy_db_snapshot(
        SourceDBSnapshotIdentifier=source_arn,
        TargetDBSnapshotIdentifier=COPY_SNAPSHOT_ID,
        KmsKeyId=dev_network["kms_key_arn"],
        SourceRegion=PROD_REGION,
        # CopyTags is rejected by the API for shared/public source snapshots.
    )
    poll_until(
        "copied snapshot",
        lambda: describe_snapshot(dev_rds, COPY_SNAPSHOT_ID),
        done=lambda status: status["status"] == "available",
        failed=lambda status: status["status"] == "failed",
    )

    log(f"restoring copied snapshot to throwaway instance {TMP_INSTANCE_ID}")
    dev_rds.restore_db_instance_from_db_snapshot(
        DBInstanceIdentifier=TMP_INSTANCE_ID,
        DBSnapshotIdentifier=COPY_SNAPSHOT_ID,
        DBInstanceClass=DEV_INSTANCE_CLASS,
        DBSubnetGroupName=dev_network["db_subnet_group_name"],
        VpcSecurityGroupIds=[dev_network["security_group_id"]],
        PubliclyAccessible=False,
        MultiAZ=False,
    )
    instance = poll_until(
        "throwaway instance restore",
        lambda: describe_instance(dev_rds, TMP_INSTANCE_ID),
        done=lambda status: status["status"] == "available",
        failed=lambda status: status["status"] == "failed",
    )
    log(f"{TMP_INSTANCE_ID} restored and available at {instance['address']}:{instance['port']}")

    try:
        # Restore inherits prod's master password -- the anonymize task
        # connects with dev's own password (same SSM parameter reset()
        # reads), so this has to happen before that, not just before swap.
        reset_dev_master_password(dev, TMP_INSTANCE_ID)
        run_anonymize_task(dev, dev_network, instance["address"], instance["port"])
    except Exception:
        log(f"anonymize failed -- deleting throwaway instance {TMP_INSTANCE_ID} (live instance untouched, safe to clean up)")
        try:
            dev_rds.delete_db_instance(DBInstanceIdentifier=TMP_INSTANCE_ID, SkipFinalSnapshot=True)
        except Exception as exc:  # noqa: BLE001
            log(f"warning: failed to clean up throwaway instance: {exc}")
        raise

    # Point of no return: from here on nothing may auto-delete anything on
    # failure -- the live instance is about to be renamed away.
    log("=== point of no return: swapping the throwaway instance in as live ===")

    log(f"renaming live instance {live_identifier} -> {previous_identifier}")
    rename_instance(dev_rds, live_identifier, previous_identifier)
    poll_until(
        "renaming old live instance aside",
        lambda: describe_instance(dev_rds, previous_identifier),
        done=lambda status: status["status"] == "available",
        failed=lambda status: status["status"] == "failed",
    )

    log(f"renaming {TMP_INSTANCE_ID} -> {live_identifier}")
    rename_instance(dev_rds, TMP_INSTANCE_ID, live_identifier)
    poll_until(
        "promoting throwaway instance to live",
        lambda: describe_instance(dev_rds, live_identifier),
        done=lambda status: status["status"] == "available",
        failed=lambda status: status["status"] == "failed",
    )

    reconcile_terraform_state(live_identifier, dev_bootstrap_env)

    log(f"deleting old instance {previous_identifier} (no retention policy yet -- always deleted after a clean swap)")
    dev_rds.delete_db_instance(DBInstanceIdentifier=previous_identifier, SkipFinalSnapshot=True)
    poll_until(
        f"deleting {previous_identifier}",
        lambda: describe_instance(dev_rds, previous_identifier)["status"],
        done=lambda status: status == "not_found",
    )

    log(f"cleanup when you're done: aws rds delete-db-snapshot --region {DEV_REGION} "
        f"--db-snapshot-identifier {COPY_SNAPSHOT_ID}")
    log("=== done ===")


if __name__ == "__main__":
    main()