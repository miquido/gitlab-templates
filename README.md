#Miquido gitlab templates


Example include:
```yaml
include:
  - project: 'miquido/dev/gitlab-templates'
    ref: 'tags/1.2.0'
    file: 'workflow-default.yml'
  - project: 'miquido/dev/gitlab-templates'
    ref: 'tags/1.2.0'
    file: 'html-dynamic-env.yml'
```