#!/usr/bin/env python3

''' Interfaces to run a target or built-in command on a set of selector '''

import multiprocessing
import copy

from time import time
from typing import Optional, Any

from .errors import RunTargetError
from .util import bash_wrap
from .tasks import Task, join_all
from .cluster import Cluster
from .config import Configuration
from .slice import Slice
from .set import MachineSet

# Can be set to let the configuration pick its default prelude
DEFAULT_PRELUDE: str = ''


def cleanup(selector, verbose: bool):
    ''' Clean up working directories on the specified selector '''

    if not isinstance(selector, (Slice, Cluster, MachineSet)):
        raise ValueError("selector is not a slice, cluster, or machine set")

    connections = []

    for minfo in selector.get_all_machines():
        if verbose:
            print(f'Cleaning up home directory "{selector.workdir}" on '
                  f'machine\"{minfo.name}"')

        # bash might not be the default shell
        cmd = bash_wrap([f"rm -rf {selector.workdir}/*"])
        machine = Task(0, minfo, "cleanup", cmd, selector.cluster,
                       verbose=verbose, username="root")
        machine.start()
        connections.append(machine)

    print(f"⌛ Waiting for {len(connections)} machine(s) to finish...")
    errors = join_all(connections)

    return len(errors) == 0


def _builtin_install_packages(selector, config: Configuration, verbose: bool,
                              use_sudo=True, dry_run=False, debug=False):
    '''
        Install all Debian packages required by the config on the specified selector

        This uses sudo by default, but you can also run as root and without sudo by
        setting sudo=False
    '''

    if not isinstance(selector, (Slice, Cluster, MachineSet)):
        raise ValueError("selector is not a slice, cluster, or machine set")

    if use_sudo:
        sudo = "sudo "
        user = None
    else:
        sudo = ""
        user = "root"

    machines = selector.get_all_machines()

    tasks = []

    repos = config.required_ubuntu_repositories
    packages = config.required_ubuntu_packages

    if len(repos) == 0 and len(packages) == 0:
        print(("No required ubuntu repositiories or packages found. "
               "Will not install anything."))
        return

    print(f'Adding {len(repos)} repositories and {len(packages)} packages '
          f' to machines {[m.name for m in machines]}')

    if dry_run:
        print("Try run was requested. Will stop here.")
        return

    for minfo in machines:
        add_repos = [sudo + "apt-add-repository " + repo for repo in repos]

        # bash might not be the default shell
        command = bash_wrap(add_repos + [
            sudo + "apt-get update",
            sudo + "DEBIAN_FRONTEND=noninteractive apt-get install -y "
                 + " ".join(packages)
        ])

        task = Task(0, minfo, "install-packages", command,
                    selector.cluster, verbose=verbose, username=user,
                    debug=debug)
        task.start()
        tasks.append(task)

    print(f"⌛ Waiting for {len(tasks)} machine(s) to finish...")
    errors = join_all(tasks)

    if len(errors) > 0:
        raise RunTargetError("install-package", errors)


def _parse_options(target, provided: Optional[dict[str, Any]]) -> tuple[list[str], str]:
    '''
        Pick default values for options or the provided value by the the
        call or run (or equivalent).a

        Note: This funciton will modify the provided options and the dict
        should not be (re-)used after passed to thic function.
    '''

    # Sorted list of options to pass to the script
    optvec = []

    # Human-readable description of the picked options
    optstr = []

    if provided and not isinstance(provided, dict):
        raise ValueError("options must be None or a dictionary")

    for option in target.options:
        value = option.default_value
        is_default = True

        if provided and option.name in provided:
            value = provided[option.name]
            del provided[option.name]
            is_default = False
        elif option.required and provided:
            raise ValueError(f'No value given for required option "{option.name}". '
                             f'Given options were "{provided.keys()}".')
        elif option.required:
            raise ValueError(f'Option "{option.name}" is required, '
                             f'but no options were set.')

        # TODO should we allow None as valid value?
        if value is None:
            raise ValueError(f'No value set for required option "{option.name}"')

        if option.value_type and not isinstance(value, option.value_type):
            raise ValueError(f'Invalid type set for option "{option.name}". '
                             f'Was `{type(value)}` but expected `{option.value_type}`.')

        if option.choices and value not in option.choices:
            raise ValueError(f'Invalid value set for option "{option.name}". '
                             f'Was `{value}` but allowed choices are '
                             f'{",".join(option.choices)}.')

        optvec.append(value)

        defaultstr = " (default)" if is_default else ""

        if isinstance(value, str):
            valstr = f'"{value}"'
        else:
            valstr = f'{value}'
        optstr.append(f'\"{option.name}\": {valstr}{defaultstr}')

    # Check if there were any invalid options specified
    if provided:
        for name in provided.keys():
            raise ValueError(f'Got unexpected option "{name}" for target '
                             f'"{target.name}". '
                             f'Allowed options are {target.option_names}')

    return (optvec, ", ".join(optstr))


def run(selector, config, target_name, options: Optional[dict[str, Any]] = None,
        verbose=False, multiply=1, prelude: Optional[str] = DEFAULT_PRELUDE,
        dry_run=False, log_dir: Optional[str] = None, timeout: Optional[float] = None,
        debug=False, workdir=None, quiet_fail=False) -> bool:
    '''
        Runs the specified command(s) in the foreground

        Arguments:
            * options: The options to set for the command
            * verbose: Print more information to stdout?
            * multiply: Run more than one task per machine?
            * quiet_fail: Disable any output to stdout/stderr when the task
                          is unsuccessful
            * dry_run: Do not actually perform the task (for testing only)
            * timeout: Give up after the specified duration (in seconds)
    '''
    try:
        check_run(selector, config, target_name, options=options, verbose=verbose,
            multiply=multiply, prelude=prelude, dry_run=dry_run,
            log_dir=log_dir, timeout=timeout, debug=debug, workdir=workdir)
        return True
    except RunTargetError as err:
        if not quiet_fail:
            print('❗' + str(err))

        return False


def check_run(selector, config, target_name, options: Optional[dict[str, Any]] = None,
        verbose=False, multiply: int = 1, prelude: Optional[str] = DEFAULT_PRELUDE,
        dry_run=False, log_dir=None, timeout=None, debug=False, workdir=None):
    '''
        This behaves like `run` but, smilar to subprocess.check_call
        will throw an exception on failure.

        The options are identical to `run`.
    '''

    if not isinstance(selector, (Slice, Cluster, MachineSet)):
        raise ValueError("selector is not a slice, cluster, or machine set")

    assert isinstance(config, Configuration)

    if multiply <= 0:
        raise ValueError("multiply option needs to be a positive number")

    if target_name == "install-packages":
        if debug:
            print('Found built-in "install-packages"')

        _builtin_install_packages(selector, config,
                   dry_run=dry_run, verbose=verbose, debug=debug)
        return

    target = config.get_target(target_name)
    if target is None:
        raise ValueError(f'No such target "{config.name}::{target_name}"')

    optvec, optstr = _parse_options(target, options)

    # Support passing none as a string for compatibility
    if prelude in ['None', 'none']:
        prelude = None

    # Get default prelude if request by user
    if prelude == DEFAULT_PRELUDE:
        prelude = config.default_prelude

    start_time = time()

    if prelude:
        prelude_txt = f' and prelude="{prelude}"'
        prelude = config.get_prelude_cmd(prelude)
    else:
        prelude_txt = ""

    machines = selector.get_all_machines()
    tasks = []

    print((f'ℹ️ Running "{config.name}::{target.name}" on {len(machines)} machine(s) '
           f'with options={{{optstr}}}') + prelude_txt)

    if dry_run:
        print("dry_run was specified, so I will stop here")
        return

    if not workdir:
        workdir = selector.workdir

    if multiply == 0:
        raise ValueError("`multiply` must be positive")

    if len(machines) == 0:
        raise ValueError("selector cannot be empty")

    # How many SSH connections in total?
    group_size = len(machines) * multiply

    for (pos, minfo) in enumerate(machines):
        for i in range(multiply):
            task = Task(pos * multiply + i, minfo,
                target.name, target.command,
                selector.cluster, options=optvec, workdir=workdir,
                verbose=verbose, grp_size=group_size,
                prelude=prelude, log_dir=log_dir, debug=debug)

            tasks.append(task)

    for task in tasks:
        task.start()

    errs = join_all(tasks, start_time=start_time, timeout=timeout)

    if len(errs) > 0:
        raise RunTargetError(target.name, errs)


def background_run(selector, config, target_name, options: Optional[dict] = None,
                   verbose=False, multiply: int = 1, log_dir=None, workdir=None,
                   prelude: Optional[str] = DEFAULT_PRELUDE,dry_run=False,
                   timeout=None, debug=False) -> multiprocessing.Process:
    '''
        Runs the specified command(s) in the background and returns a
        multiprocessing.Process object to manage the background task.

        The options are idenitical to `run` and `check_run`
    '''

    # Check options before spawning the new process
    assert isinstance(config, Configuration)
    target = config.get_target(target_name)
    if target is None:
        raise ValueError(f'No such target "{config.name}::{target_name}"')

    # Will throw an error if the options are invalid
    # Copy here so the original options dict is not modified
    _parse_options(target, copy.copy(options))

    proc = multiprocessing.Process(target=run, args=(selector, config, target_name),
                                   kwargs={'verbose': verbose, 'multiply': multiply,
                                           'log_dir': log_dir, 'workdir': workdir,
                                           'prelude': prelude, 'dry_run': dry_run,
                                           'timeout': timeout, 'debug': debug,
                                           'options': options,
                                           })
    proc.start()
    return proc
