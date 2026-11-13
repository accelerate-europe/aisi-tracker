# Dependencies and pinning

Every command in the release and maintenance process runs from a lockfile, so the same commit gives the same packages and the same browser on every host and in CI. This page is the rule. `scripts/dependency_checks.py` checks the static part (every header has its lock, a script that imports Playwright has both, heavy dependencies are pinned exactly, the locks agree, and every command in the docs, workflows, hooks, scripts and tests has the locked form) in `check.py`'s lint step, so in `--only fast` and the pre-commit hook.

## Two kinds of environment

- The project environment: `pyproject.toml` and `uv.lock`. A script without a PEP 723 header runs here, and so do the tools (`pytest`, `ruff`).
- A script environment: a PEP 723 header (`# /// script`) and its lock beside it, `scripts/<name>.py.lock`.

A script has a header only when it needs packages outside the project environment (Playwright, geodata) or must run before the project environment is installed (`check.py`, which CI runs first). A script with a header always has a lock, including a header with no dependencies. A script without a header has neither.

## Running a script

- Scripts: `uv run --locked scripts/<name>.py`, in docs, runbooks, agent instructions, hooks, CI and scripts that call each other. It works for both kinds of script, and it refuses to run when a lock is missing or out of date instead of resolving packages afresh.
- Tools: `uv run --locked <tool>`.
- Not allowed: `uv run python scripts/<name>.py` (it ignores the script's header and lock), `--with`, `--no-project --with`, `--group`, `pip install`, or a version range on the command line. A one-off tool that needs a package not in any lock is either added to a lock in a reviewed commit or is not part of the process.
- The exception is the two git hooks that pipe `check.py` into `uv run --no-project python -` (pre-commit and pre-push): they use the standard library only, so there is nothing to pin.

## Versions

- A header pins each heavy dependency exactly (`playwright==1.63.0`); light dependencies may keep a lower bound, since the lock fixes the version.
- One version per package across all locks, direct and transitive: for any one Python version, every lock that contains a package resolves it to the same version. So every lock that contains Playwright has the same Playwright, and a lock that splits by Python version (`resolution-markers`) splits the same way as the others that share the package.
- Cooldown: resolution leaves out releases less than 7 days old. Each lock records this (`exclude-newer-span = "P7D"`), CI sets `UV_EXCLUDE_NEWER: "7 days"`, and maintainers' uv is configured the same way. A security fix younger than that is taken with a per-package exemption (`exclude-newer-package` under `[tool.uv]` in `pyproject.toml` or the script's header), named with its advisory in the commit message and removed once the window has passed.
- uv itself: CI pins the uv release the locks were checked with (`setup-uv` in `.github/workflows/pages.yml`); hosts use that release or a newer one.

## Changing a lock

A new script's first lock travels with the script. A change to an existing lock goes in its own reviewed commit (`Deps: ...`), never inside a data, page or feature commit.

1. Change the header or `pyproject.toml`, then relock every lock that holds the package: `uv lock` and `uv lock --script scripts/<name>.py` for each script lock. A script whose own dependencies pull in packages that `uv.lock` also holds keeps them at `uv.lock`'s versions with `constraint-dependencies` in its header (`slide_map.py`); move those pins with `uv.lock`.
2. When Playwright changes, install the matching Chromium in the same step: `uv run --locked scripts/check_page.py --install-chrome`. Every host that runs browser steps does this after pulling the commit; CI does it in every browser job.
3. Run the fast checks and push; CI's build and browser gate must pass before the commit is merged.

Locks change when there is a reason (a fix, a security advisory, a feature the scripts need), not on a schedule.

## Browser

The only browser the build, the browser gate, the contact sheet and the source monitor use is the Chromium that the locked Playwright installs: no system Chromium, and no `executable_path` other than the locked Playwright's own. `check.py --preflight` and every browser step (the gate, the build's pre-render, the contact sheet's capture and the source monitor) check for the Chromium build the locked Playwright expects and, when it is missing, report NOT RUN naming that build, the builds that are installed and the install command (`scripts/host_checks.py`). A second preflight row and the gate's start refuse more parallel browser jobs than the host's memory holds at 4 GiB each, with the `AISI_GATE_JOBS` value that fits.
