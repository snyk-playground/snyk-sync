from pydantic.typing import update_field_forward_refs
import typer
import time
import os
import json
import yaml
import snyk
import api
import logging


from __version__ import __version__

from os import environ

from pathlib import Path
from github import Github, Repository
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from models.repositories import Repo, Project, Tag
from models.sync import SnykWatchList, Settings
from models.organizations import Orgs, Org, Target

from utils import yopen, jopen, search_projects, RateLimit, newer, jwrite

app = typer.Typer(add_completion=False)

s = Settings()

watchlist = SnykWatchList()

# DEBUG_LEVEL = environ["SNYK_SYNC_DEBUG_LEVEL"] or "INFO"

logging.basicConfig(level="INFO")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    cache_dir: Optional[Path] = typer.Option(
        default=None,
        exists=True,
        file_okay=False,
        dir_okay=True,
        writable=True,
        readable=True,
        resolve_path=True,
        help="Cache location",
        envvar="SNYK_SYNC_CACHE_DIR",
    ),
    cache_timeout: int = typer.Option(
        default=60,
        help="Maximum cache age, in minutes",
        envvar="SNYK_SYNC_CACHE_TIMEOUT",
    ),
    forks: bool = typer.Option(
        default=False,
        help="Check forks for import.yaml files",
        envvar="SNYK_SYNC_FORKS",
    ),
    conf: Path = typer.Option(
        default="snyk-sync.yaml",
        exists=True,
        file_okay=True,
        dir_okay=False,
        writable=False,
        readable=True,
        resolve_path=True,
        envvar="SNYK_SYNC_CONFIG",
    ),
    targets_file: Optional[Path] = typer.Option(
        default=None,
        exists=True,
        file_okay=True,
        dir_okay=False,
        writable=True,
        readable=True,
        resolve_path=True,
        envvar="SNYK_SYNC_TARGETS_FILE",
    ),
    snyk_orgs_file: Optional[Path] = typer.Option(
        default=None,
        exists=True,
        file_okay=True,
        dir_okay=False,
        writable=False,
        readable=True,
        resolve_path=True,
        help="Snyk orgs to watch",
        envvar="SNYK_SYNC_ORGS",
    ),
    default_org: str = typer.Option(
        default=None,
        help="Default Snyk Org to use from Orgs file.",
        envvar="SNYK_SYNC_DEFAULT_ORG",
    ),
    default_int: str = typer.Option(
        default=None,
        help="Default Snyk Integration to use with Default Org.",
        envvar="SNYK_SYNC_DEFAULT_INT",
    ),
    instance: str = typer.Option(
        default=None,
        help="Default Snyk Integration to use with Default Org.",
        envvar="SNYK_SYNC_INSTANCE",
    ),
    snyk_group: UUID = typer.Option(
        default=None,
        help="Group ID, required but will scrape from ENV",
        envvar="SNYK_SYNC_GROUP",
    ),
    snyk_token: UUID = typer.Option(
        ...,
        help="Snyk access token",
        envvar="SNYK_TOKEN",
    ),
    force_sync: bool = typer.Option(False, "--sync", help="Forces a sync regardless of cache status"),
    github_token: str = typer.Option(
        ...,
        help="GitHub access token",
        envvar="GITHUB_TOKEN",
    ),
):

    # We keep this as the global settings hash
    global s
    global watchlist

    # s_dict = dict.fromkeys([o for o in dir() if o != 'ctx'])

    # for k in s_dict:
    #     s_dict[k] = vars()[k]

    # why are we creating a dict and then loading it?

    # updating a global var
    # this is a lazy way of stripping all the data from the inputs into the settings we care about
    s = Settings.parse_obj(vars())

    conf_dir = os.path.dirname(str(s.conf))

    conf_file = yopen(s.conf)

    if s.targets_file is None:
        if "targets_file_name" in conf_file:
            s.targets_file = Path(f'{conf_dir}/{conf_file["targets_file_name"]}')
        else:
            s.targets_file = Path(f"{conf_dir}/import-targets.json")

    if s.cache_dir is None:
        if "cache_dir" in conf_file:
            s.cache_dir = Path(f'{conf_dir}/{conf_file["targets_file_name"]}')
        else:
            s.cache_dir = Path(f"{conf_dir}/cache")

    if not s.cache_dir.exists() and not s.cache_dir.is_dir():
        s.cache_dir.mkdir()
    elif not s.cache_dir.is_dir():
        typer.Abort(f"{s.cache_dir.name} is not a directory")

    if s.snyk_orgs_file is None:
        if "orgs_file" in conf_file:
            s.snyk_orgs_file = Path(f'{conf_dir}/{conf_file["orgs_file"]}')
        else:
            s.snyk_orgs_file = Path(f"{conf_dir}/snyk-orgs.yaml")

    s.github_orgs = conf_file["github_orgs"]

    if s.default_org is None:
        s.default_org = conf_file["default"]["orgName"]

    if s.default_int is None:
        s.default_int = conf_file["default"]["integrationName"]

    if s.snyk_groups is None:
        s.snyk_groups = conf_file["snyk"]["groups"]

    if s.instance is None:
        if "instance" in conf_file.keys():
            s.instance = conf_file["instance"]

    s.snyk_orgs = yopen(s.snyk_orgs_file)

    s.default_org_id = s.snyk_orgs[s.default_org]["orgId"]
    s.default_int_id = s.snyk_orgs[s.default_org]["integrations"][s.default_int]

    if ctx.invoked_subcommand is None:
        typer.echo("Snyk Sync invoked with no subcommand, executing all", err=True)
        if status() is False:
            sync()


@app.command()
def sync(
    show_rate_limit: bool = typer.Option(
        False,
        "--show-rate-limit",
        help="Display GH rate limit status between each batch of API calls",
    )
):
    """
    Force a sync of the local cache of the GitHub / Snyk data.
    """

    global watchlist
    global s

    typer.echo("Sync starting", err=True)

    # flush the watchlist
    # watchlist = SnykWatchList()

    gh = Github(s.github_token, per_page=100)

    rate_limit = RateLimit(gh)

    client = snyk.SnykClient(
        str(s.snyk_token), user_agent=f"pysnyk/snyk_services/sync/{__version__}", tries=2, delay=1
    )

    v3client = api.SnykV3Client(
        str(s.snyk_token), user_agent=f"pysnyk/snyk_services/sync/{__version__}", tries=2, delay=1
    )

    if s.github_orgs is not None:
        gh_orgs = list(s.github_orgs)
    else:
        gh_orgs = list()

    rate_limit.update(show_rate_limit)

    exclude_list = []

    typer.echo("Getting all GitHub repos", err=True)

    for gh_org_name in gh_orgs:
        gh_org = gh.get_organization(gh_org_name)
        gh_repos = gh_org.get_repos(type="all", sort="updated", direction="desc")
        gh_repos_count = gh_repos.totalCount
        with typer.progressbar(length=gh_repos_count, label=f"Processing {gh_org_name}: ") as gh_progress:
            for gh_repo in gh_repos:

                watchlist.add_repo(gh_repo)

                gh_progress.update(1)

    # print(exclude_list)
    rate_limit.update(show_rate_limit)

    import_yamls = []
    for gh_org in gh_orgs:
        search = f"org:{gh_org} path:.snyk.d filename:import language:yaml"
        import_repos = gh.search_code(query=search)
        import_repos = [y for y in import_repos if y.repository.id not in exclude_list and y.name == "import.yaml"]
        import_yamls.extend(import_repos)

    rate_limit.update(show_rate_limit)

    # we will likely want to put a limit around this, as we need to walk forked repose and try to get import.yaml
    # since github won't index a fork if it has less stars than upstream

    forks = [f for f in watchlist.repos if f.fork()]
    forks = [y for y in forks if y.id not in exclude_list]

    if s.forks is True and len(forks) > 0:
        typer.echo(f"Scanning {len(forks)} forks for import.yaml", err=True)

        with typer.progressbar(forks, label="Scanning: ") as forks_progress:
            for fork in forks_progress:
                f_owner = fork.source.owner
                f_name = fork.source.name
                f_repo = gh.get_repo(f"{f_owner}/{f_name}")
                try:
                    f_yaml = f_repo.get_contents(".snyk.d/import.yaml")
                    watchlist.get_repo(f_repo.id).parse_import(f_yaml, instance=s.instance)
                except:
                    pass

        typer.echo(f"Have {len(import_yamls)} Repos with an import.yaml", err=True)
        rate_limit.update(show_rate_limit)

    if len(import_yamls) > 0:
        typer.echo(f"Loading import.yaml for non fork-ed repos", err=True)

        with typer.progressbar(import_yamls, label="Scanning: ") as import_progress:
            for import_yaml in import_progress:

                r_id = import_yaml.repository.id

                import_repo = watchlist.get_repo(r_id)

                import_repo.parse_import(import_yaml, instance=s.instance)

    rate_limit.update(show_rate_limit)

    # this calls our new Orgs object which caches and populates Snyk data locally for us

    all_orgs = Orgs(cache=str(s.cache_dir), groups=s.snyk_groups)

    select_orgs = [str(o["orgId"]) for k, o in s.snyk_orgs.items()]

    typer.echo(f"Updating cache of Snyk projects", err=True)

    all_orgs.refresh_orgs(client, v3client, origin="github-enterprise", selected_orgs=select_orgs)

    all_orgs.save()

    typer.echo("Scanning Snyk for projects originating from GitHub Enterprise Repos", err=True)
    for r in watchlist.repos:
        found_projects = all_orgs.find_projects_by_repo(r.full_name, r.id)

        for p in found_projects:
            r.add_project(p)

    watchlist.save(cachedir=str(s.cache_dir))
    typer.echo("Sync completed", err=True)

    if show_rate_limit is True:
        rate_limit.total()

    typer.echo(f"Total Repos: {len(watchlist.repos)}", err=True)


@app.command()
def status():
    """
    Return if the cache is out of date
    """
    global watchlist
    global s

    if s.force_sync:
        typer.echo("Sync forced, ignoring cache status", err=True)
        return False

    typer.echo("Checking cache status", err=True)

    if os.path.exists(f"{s.cache_dir}/sync.json"):
        sync_data = jopen(f"{s.cache_dir}/sync.json")
    else:
        return False

    last_sync = datetime.strptime(sync_data["last_sync"], "%Y-%m-%dT%H:%M:%S.%f")

    in_sync = True

    if s.cache_timeout is None:
        timeout = 0
    else:
        timeout = float(str(s.cache_timeout))

    if last_sync < datetime.utcnow() - timedelta(minutes=timeout):
        typer.echo("Cache is out of date and needs to be updated", err=True)
        in_sync = False
    else:
        typer.echo(f"Cache is less than {s.cache_timeout} minutes old", err=True)

    typer.echo("Attempting to load cache", err=True)
    try:
        cache_data = jopen(f"{s.cache_dir}/data.json")
        for r in cache_data:
            watchlist.repos.append(Repo.parse_obj(r))

    except KeyError as e:
        typer.echo(e)

    typer.echo("Cache loaded successfully", err=True)

    return in_sync


@app.command()
def targets(
    save_targets: bool = typer.Option(False, "--save", help="Write targets to disk, otherwise print to stdout")
):
    """
    Returns valid input for api-import to consume
    """
    global s
    global watchlist

    if status() == False:
        sync()

    target_list = []

    for r in watchlist.repos:
        if len(r.projects) == 0 or r.needs_reimport(s.default_org, s.snyk_orgs) is True:
            if r.org != "default":
                org_id = s.snyk_orgs[r.org]["orgId"]
                int_id = s.snyk_orgs[r.org]["integrations"]["github-enterprise"]
            else:
                org_id = s.default_org_id
                int_id = s.default_int_id

            for branch in r.branches:
                source = r.source.get_target()

                source["branch"] = branch

                target = {
                    "target": source,
                    "integrationId": int_id,
                    "orgId": org_id,
                }

                target_list.append(target)

    import_targets = {"targets": target_list}

    if save_targets is True:
        typer.echo(f"Writing targets to {s.targets_file}", err=True)
        if jwrite(import_targets, s.targets_file):
            typer.echo("Write Successful", err=True)
        else:
            typer.echo("Write Failed", err=True)
    else:
        typer.echo(json.dumps(import_targets, indent=2))


@app.command()
def tags(
    update_tags: bool = typer.Option(False, "--update", help="Updates tags on projects instead of outputting them"),
    save_tags: bool = typer.Option(False, "--save", help="Write tags to disk, otherwise print to stdout"),
):
    """
    Returns list of project id's and the tags said projects are missing
    """
    global s
    global watchlist

    if status() is False:
        sync()

    has_tags = [r for r in watchlist.repos if r.has_tags()]

    needs_tags = []

    for repo in has_tags:
        for project in repo.projects:
            missing_tags = project.get_missing_tags(repo.org, repo.tags)
            if len(missing_tags) > 0:
                missing_tags = [m.dict() for m in missing_tags]
                fix_project = {
                    "org_id": str(project.org_id),
                    "project_id": str(project.id),
                    "tags": missing_tags,
                }
                needs_tags.append(fix_project)

    conf_dir = os.path.dirname(str(s.conf))

    # tags should be broken out into it's own module

    if len(needs_tags) > 0:
        if update_tags is True:
            typer.echo(f"Updating tags for projects", err=True)
            client = snyk.SnykClient(str(s.snyk_token))
            for p in needs_tags:
                p_path = f"org/{p['org_id']}/project/{p['project_id']}"
                p_tag_path = f"org/{p['org_id']}/project/{p['project_id']}/tags"

                p_live = json.loads(client.get(p_path).text)

                for tag in p["tags"]:
                    if tag not in p_live["tags"]:
                        client.post(p_tag_path, tag)
                    else:
                        typer.echo("Tag already updated", err=True)
        elif save_tags is True:
            typer.echo(f"Writing tag updates to {conf_dir}/tag-updates.json")
            if jwrite(needs_tags, f"{conf_dir}/tag-updates.json"):
                typer.echo("Write Successful", err=True)
            else:
                typer.echo("Write Failed", err=True)
        else:
            typer.echo(json.dumps(needs_tags, indent=2))
    else:
        typer.echo("No projects require tag updates", err=True)


@app.command()
def autoconf(
    snykorg: str = typer.Argument(..., help="The Snyk Org Slug to use"),
    githuborg: str = typer.Argument(..., help="The Github Org to use"),
):
    """
    Autogenerates a configuration template given an orgname

    This requires an existing snyk-sync.yaml and snyk-orgs.yaml, which it will overwrite
    """
    global s

    client = snyk.SnykClient(str(s.snyk_token), user_agent=f"pysnyk/snyk_services/sync/{__version__}")

    conf = dict()
    conf["schema"] = 1
    conf["github_orgs"] = [str(githuborg)]
    conf["snyk"] = dict()
    conf["snyk"]["group"] = None
    conf["default"] = dict()
    conf["default"]["orgName"] = snykorg
    conf["default"]["integrationName"] = "github-enterprise"

    orgs = json.loads(client.get("orgs").text)

    my_org = [o for o in orgs["orgs"] if o["slug"] == snykorg][0]

    my_group_id = my_org["group"]["id"]

    group_orgs = json.loads(client.get(f"group/{my_group_id}/orgs").text)["orgs"]

    snyk_orgs = dict()
    for org in group_orgs:
        org_int = json.loads(client.get(f"org/{org['id']}/integrations").text)

        if "github-enterprise" in org_int:
            snyk_orgs[org["slug"]] = dict()
            snyk_orgs[org["slug"]]["orgId"] = org["id"]
            snyk_orgs[org["slug"]]["integrations"] = org_int

    s.conf.write_text(yaml.safe_dump(conf))
    s.snyk_orgs_file.write_text(yaml.safe_dump(snyk_orgs))


if __name__ == "__main__":
    app()
