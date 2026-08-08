# Command Line

The public command is `slime`. Windows and Linux both use
`src/slime_cairn/cli.py`; the checked-in `slime`/`slime.cmd` launchers only set
the project root and Python import path before invoking that module.

From the repository root, the checked-in launchers work without installation:

```powershell
.\slime help
.\slime up
```

On Linux, use `sh ./slime help` when the checkout does not preserve executable
bits. After installation, the generated `~/.local/bin/slime` is executable.

The Windows launcher uses `python` and the Linux launcher uses `python3` by
default. Install the project into the Python environment that should own Slime
before starting services:

```sh
python3 -m pip install -e .
```

Set `SLIME_PYTHON` to an explicit interpreter path when using a virtualenv or
a non-default Python installation.

Install the short command once and open a new terminal:

```text
# Windows PowerShell
.\slime install

# Linux shell
sh ./slime install

# Both platforms, after opening a new terminal
slime help
```

On Windows, installation adds only this repository's `bin` directory to the
current user's `PATH`. On Linux, it creates a managed `~/.local/bin/slime`
launcher and adds `~/.local/bin` to `~/.profile` when needed. It does not
install a system service, start Slime, or modify the machine-wide `PATH`.
Remove the managed user command with:

```text
slime uninstall
```

Use `-DryRun` on Windows or `--dry-run` on Linux with `install` or `uninstall`
to inspect the PATH change without applying it.

## Command Groups

| Area | Commands |
|---|---|
| Service | `up`, `down`, `restart`, `doctor`, `ui` |
| Project | `new`, `list`, `status`, `runtime`, `pause`, `resume`, `stop`, `delete` |
| Observation | `logs`, `logs -Follow` |
| Foreground development | `serve`, `dispatch` |
| Command setup | `install`, `uninstall`, `help` |

Run `slime help` for examples. The same parser accepts PowerShell-style options
such as `-Name` and POSIX-style options such as `--name` on both platforms.
