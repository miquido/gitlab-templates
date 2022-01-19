# Miquido gitlab templates

Example include:
```yaml

include:
  - remote: 'https://raw.githubusercontent.com/miquido/gitlab-templates/1.3.54/workflow-default.yml'
  - remote: 'https://raw.githubusercontent.com/miquido/gitlab-templates/1.3.54/terraform-toolkit.yml'
  - remote: 'https://raw.githubusercontent.com/miquido/gitlab-templates/1.3.54/git-toolkit.yml'
  - remote: 'https://raw.githubusercontent.com/miquido/gitlab-templates/1.3.54/html-dynamic-env.yml'
```

## Important

Pay attention that include files points to public [**github repository**](https://github.com/miquido/gitlab-templates) not private gitlab repository.


## Git toolkit

### simple_changelog
Writes commit messages that are present from the previous build. (using `CI_COMMIT_BEFORE_SHA` variable)

```yaml
include:
  - remote: 'https://raw.githubusercontent.com/miquido/gitlab-templates/1.3.54/git-toolkit.yml

changelog:
  extends: .simple_changelog

read-changelog:
  stage: .post
  script:
    - cat changelog.txt
  tags:
    - miquido
    - docker


```
### Full changelog
Using [git-chglog](https://github.com/git-chglog/git-chglog) Will generate rich changelog with grouped commits. It provides also jira integration

To take advantage of changelog features, commit headers must match:
```shell
feat(core)[XD-42]: Added feature
```
where
- `feat` is type of commit
- `core` is a scope of commit
- `XD-42` is a jira issue id
- `Added feature` is a subject of the commit.

Every element can be ommited so commit message could have no jira issue or scope attached. For example:
```shell
fix: Fixed bug
```

Usage:

To provide what should be included into changelog, please provide `TAG_QUERY` [format](https://github.com/git-chglog/git-chglog#tag-query)

```yaml
latest-changelog:
  extends: .changelog
  variables:
#    Changelog filepath. Will be exposed as artifact. Defaults: CHANGELOG.md
    CHANGELOG_FILE: "CHANGELOG.md"
#    Set for what commits should the changelog be generated. Defaults: empty
    TAG_QUERY: $CI_COMMIT_TAG
#    For jira integration
    JIRA_URL: https://miquido.atlassian.net
    JIRA_USERNAME: user@example.com
    JIRA_TOKEN: <jira_access_token>

```

### Push Changelog

If `CHANGELOG.md` is modified and present in job artifacts, it can be automatically push on the main branch.
it produces commit message ending with `[skip-ci]`

To prevent running gitlab CI for this commit, plese add such workfow to your `.gitlab-ci.yml`
```yaml
workflow:
  rules:
    - if: $CI_COMMIT_MESSAGE =~ /^.*\[skip-ci\]$/
      when: never
```
Usage:
```yaml
push-changelog:
  extends: .push-changelog
```

## Gitlab Toolkit

### Gitlab Release
It takes file `RELEASE_DESCRIPTION_FILE` and creates A release from `CI_COMMIT_TAG`. It runs only on tags by default

Usage:
```yaml
gitlab-release:
  extends: .gitlab-release
  variables:
#    Optional. Adds link entry into release
    LINK_NAME: "Pypi"
    LINK_URL: "https://pypi.org/project/miquido-gitlab-releaser/$CI_COMMIT_TAG/"
#    File that has a release description. Defaults to CHANGELOG.md
    RELEASE_DESCRIPTION_FILE: CHANGELOG.md

```
