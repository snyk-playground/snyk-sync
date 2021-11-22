# Snyk Sync

A way to ensure your GitHub Repos containing projects that can be monitored by Snyk are infact monitored by Snyk.

## How does this work?

Snyk Sync connects to GitHub to retrieve a list of repositories from one or more GitHub organizations, cross references that list with the projects it can detect in a Snyk Group, and generates a list of Targets for [Snyk API Import](https://github.com/snyk-tech-services/snyk-api-import) to have Snyk attempt to monitor those unmonitored repositories.

Snyk Sync will check if a repository has a file [import.yaml](https://github.com/snyk-playground/org-project-import/blob/main/.snyk.d/import.yaml) in the root directory `.snyk.d/` this file specifies the Snyk Organization that any projects imported from the repository will be added to and any tags to ensure are added to those projects.

If there is no `import.yaml` file or the organization specified is not in the [snyk-orgs.yaml](conf/snyk-orgs.yaml) approved list the projects will go to the default organization as configured in the [snyk-sync.yaml](conf/snyk-sync.yaml) file.

Snyk Sync can be run by hand, by a scheduler, or in a github workflow (see: [config-repo](https://github.com/snyk-playground/config-repo) for a github workflow implementation)

Assumptions:

- A repository is considered monitored if it already has a single project (there are tools such as [scm-refresh](https://github.com/snyk-tech-services/snyk-scm-refresh) that will allow one to reprocess existing repositories and it is on the Snyk roadmap to reprocess them natively)
- Tags are additive: Any tags specified in the `import.yaml` will be added to all projects from the same repository. If the tag already exists as an exact match, it will not be added, and existing tags not declared in `import.yaml` will not be removed. Snyk allows for duplicate Key names, so "application:database" and "application:frontend" are both valid K:V tags that could be on the same project. This is not a suggestion to do this, but pointing out it is possible.
- Forks: Because of how GitHub's indexing works, it will not search forks. Snyk Sync uses GitHub's search functionality to detect `import.yaml` files (to keep API calls to a minimum). In order to add forks, use the `--forks` flag to have Snyk Sync search each fork individually for the `import.yaml` file. **CAUTION:** This will incur an API cost of atleast one request per fork and two if the fork contains an `import.yaml` - Snyk Sync does not have request throtlling at the moment

## Caching

If one has a large organization with many hundreds or thousands of repositories, the process of discovering all of them can be timeconsuming. In order to speed up this process, Snyk Sync builds a 'watchlist' in a cache directory (by default `cache`). It will only perform a sync (querying both GitHub and Snyk APIs) if the data is more than 60 minutes old (change with: --cache-timeout) or a sync is forced (`--sync`). This allows for the `targets` and `tags` subcommands to operate much more quickly. Depending on the size of the targets list given to snyk-api-import, it may take a long time for the project imports to complete, after which another sync should be performed and the `tags` command run to ensure any new projects that didn't exist before are now updated with their associated tags.

## Setup

Snyk Sync expects a `GITHUB_TOKEN` and `SNYK_TOKEN` environment variables to be present, along with a snyk-sync.yaml file, snyk-orgs.yaml file, and a folder to store the cache in (it will not create this folder). See the [example](/example) directory for a starting point.

```
example
├── cache
├── snyk-orgs.yaml
└── snyk-sync.yaml
```

- GITHUB_TOKEN: this access token must have read access to all repositories in all GitHub organizations one wishes to import
- SNYK_TOKEN: this should be a group level service account that has admin access to create new projects and tag them

Minimum snyk-sync.yaml contents:

```
---
schema: 1
github_orgs:
  - <<Name of GitHub Org>>
snyk:
  group: <<Group ID from Snyk>>
default:
  orgName: ie-playground
  integrationName: github-enterprise
```

Example minimum snyk-orgs.yaml:

```
---
ie-playground:
  orgId: 39ddc762-b1b9-41ce-ab42-defbe4575bd6
  integrations:
    github-enterprise: b87e1473-37ab-4f09-a4e3-a0139a50e81e
```

To get the Organization ID, navigate to the settings page of the organization in question
`https://app.snyk.io/org/<org-name>/manage/settings`

To get the GitHub Enterprise integration ID (currently the GitHub Enterprise integration is the only supported integration for snyk sync, but it can be used with a GitHub.com Org as well) navigate to:
`https://app.snyk.io/org/<org-name>/manage/integrations/github-enterprise`

### Help

Base snyk-sync flags/environment variables

```
Usage: cli.py [OPTIONS] COMMAND [ARGS]...

Options:
  --cache-dir DIRECTORY    Cache location  [env var: SNYK_SYNC_CACHE_DIR;
                           default: cache]
  --cache-timeout INTEGER  Maximum cache age, in minutes  [env var:
                           SNYK_SYNC_CACHE_TIMEOUT; default: 60]
  --forks / --no-forks     Check forks for import.yaml files  [env var:
                           SNYK_SYNC_FORKS; default: no-forks]
  --conf FILE              [env var: SNYK_SYNC_CONFIG; default: snyk-
                           sync.yaml]
  --targets-file FILE      [env var: SNYK_SYNC_TARGETS_FILE]
  --snyk-orgs-file FILE    Snyk orgs to watch  [env var: SNYK_SYNC_ORGS]
  --default-org TEXT       Default Snyk Org to use from Orgs file.  [env var:
                           SNYK_SYNC_DEFAULT_ORG]
  --default-int TEXT       Default Snyk Integration to use with Default Org.
                           [env var: SNYK_SYNC_DEFAULT_INT]
  --snyk-group UUID        Group ID, required but will scrape from ENV  [env
                           var: SNYK_SYNC_GROUP; required]
  --snyk-token UUID        Snyk access token  [env var: SNYK_TOKEN; required]
  --sync                   Forces a sync regardless of cache status
  --github-token TEXT      GitHub access token  [env var: GITHUB_TOKEN;
                           required]
  --help                   Show this message and exit.

Commands:
  status   Return if the cache is out of date
  sync     Force a sync of the local cache of the GitHub / Snyk data.
  tags     Returns list of project id's and the tags said projects are...
  targets  Returns valid input for api-import to consume
```

targets command:
Outputs the list of targets to stdout or saves them to a file. The output is formated json that [snyk-api-import](https://github.com/snyk-tech-services/snyk-api-import) accepts.

```
Usage: cli.py targets [OPTIONS]

  Returns valid input for api-import to consume

Options:
  --save  Write targets to disk, otherwise print to stdout
  --help  Show this message and exit.
```

```
Usage: cli.py tags [OPTIONS]

  Returns list of project id's and the tags said projects are missing

Options:
  --update  Updates tags on projects instead of outputting them
  --save    Write tags to disk, otherwise print to stdout
  --help    Show this message and exit.
```

### Container Build Steps

This pushes to GitHub's [container registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

```
docker build --force-rm -f Dockerfile -t snyk-sync:latest .
docker tag snyk-sync:latest ghcr.io/snyk-playground/snyk-sync:latest
docker push ghcr.io/snyk-playground/snyk-sync:latest
```

### Container Run Steps

```
docker pull ghcr.io/snyk-playground/snyk-sync:latest
docker tag ghcr.io/snyk-playground/snyk-sync:latest snyk-sync:latest
docker run --rm -it -e GITHUB_TOKEN -e SNYK_TOKEN -v "${PWD}":/runtime snyk-sync:latest --sync target
```

### Testing Rate Limits

In order to determine where a ratelimit is being reached with GitHub, we have the Github API calls extracted so they can be run and counted:

```
docker run --rm -it -e GITHUB_TOKEN -e SNYK_TOKEN \
-v "${PWD}":/runtime --entrypoint "/usr/local/bin/rate_limits.sh" \
snyk-sync:latest --conf conf/snyk-sync.yaml --per-page 100

Core Limit: 5000
Search Limit: 30
Results per page: 100
These are API calls: all repos for each org in config
        Getting GH Org for snyk-playground
                Core Cost: 1    Search Cost: 0
        Getting all repos for snyk-playground
                Core Cost: 0    Search Cost: 0
        Total repos for snyk-playground: 29
                Core Cost: 1    Search Cost: 0

API calls for searching for import.yaml
        Performing import.yaml search across snyk-playground
                Core Cost: 0    Search Cost: 0
        Total repos for snyk-playground with a import.yaml hit: 6
                Core Cost: 0    Search Cost: 1
        Filtering the list of import.yaml matches for snyk-playground
                Core Cost: 0    Search Cost: 1
```

docker run --rm -it -e GITHUB_TOKEN -e SNYK_TOKEN \
-e REQUESTS_CA_BUNDLE=/runtime/massmutual.pem -v "${PWD}":/runtime \
ghcr.io/snyk-playground/snyk-sync:latest --sync targets --save
