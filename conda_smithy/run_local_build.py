#!/usr/bin/env python
from __future__ import print_function
import os
import os.path as op
import datetime
from collections import namedtuple
from subprocess import call, check_call
import multiprocessing.dummy

from ruamel import yaml


def _ensure_writable_dir(path0, *parts):
    """Ensures that a path is a writable directory."""

    path = op.expanduser(op.join(path0, *parts))

    def check_path(path):
        if not op.isdir(path):
            raise Exception('%s exists but it is not a directory' % path)
        if not os.access(path, os.W_OK):
            raise Exception('%s is a directory but it is not writable' % path)
    if op.exists(path):
        check_path(path)
    else:
        try:
            os.makedirs(path)
        except Exception:
            if op.exists(path):  # Simpler than using a file lock to work on multithreading...
                check_path(path)
            else:
                raise
    return path


# --- Run linux builds locally

_LinuxJob = namedtuple('LinuxJob', ('recipe_root', 'name', 'envvars', 'docker_executable', 'docker_image'))


def _collect_docker_info(recipe_root):
    """Collects docker information from conda_forge.yml."""
    try:
        with open(op.join(recipe_root, 'conda-forge.yml'), 'rt') as reader:
            conda_forge_cfg = yaml.load(reader, Loader=yaml.CLoader)
            docker_executable = conda_forge_cfg.get('docker', {}).get('executable', 'docker')
            docker_image = conda_forge_cfg.get('docker', {}).get('image', 'condaforge/linux-anvil')
    except IOError:
        docker_executable = 'docker'
        docker_image = 'condaforge/linux-anvil'

    return docker_executable, docker_image


def _collect_linux_jobs(recipe_root):

    docker_executable, docker_image = _collect_docker_info(recipe_root)

    with open(op.join(recipe_root, '.circleci', 'config.yml'), 'rt') as reader:
        circleci_config = yaml.load(reader, Loader=yaml.RoundTripLoader)  # roundrip => we keep order of jobs
    jobs = []
    for job_name, jobconfig in sorted(circleci_config['jobs'].items(), reverse=True):
        job_envvars = [envvar.popitem() for envvar in jobconfig.get('environment', ())]
        jobs.append(_LinuxJob(recipe_root,
                              job_name, job_envvars,
                              docker_executable, docker_image))

    return jobs


def _run_one_linux_job(job):
    env = os.environ.copy()
    for k, v in job.envvars:
        env[k] = v
    logs_dir = _ensure_writable_dir(job.recipe_root, 'build_artefacts', 'build_logs', 'linux')
    timestamp = str(datetime.datetime.now()).replace(' ', '_').replace(':', '-').replace('.', '-')
    log_file = op.join(logs_dir, job.name + '.' + timestamp)

    with open(log_file, 'wt', 2) as log_writer:
        retcode = call(
            op.join(job.recipe_root, 'ci_support', 'run_docker_build.sh'),
            env=env,
            stdout=log_writer,
            stderr=log_writer,
        )
        print('JOB %s %s' % (job.name, 'FAIL' if retcode else 'OK'))
        return job, retcode, log_file


def run_linux_local(recipe_root='.', no_rerender=False, no_docker_pull=False, n_threads=1, safe=True, only=()):

    # Rerender
    if not no_rerender:
        try:
            check_call(['conda-smithy', 'rerender', '--feedstock_directory', recipe_root])
        except OSError:
            print('WARNING: could not rerender the recipe, is "conda-smithy" available?')

    # Collect jobs
    jobs = _collect_linux_jobs(recipe_root)

    # Filter jobs
    if only:
        jobs = [job for i, job in enumerate(jobs) if job.name in only or str(i) in only or i in only]

    # Anything to do?
    if not jobs:
        print('No jobs, exiting')
        return []
    print('Will build:')
    print(jobs)
    print('-' * 80)

    # Do run docker pull, but not in parallel
    if not no_docker_pull:
        images = sorted(set((job.docker_executable, job.docker_image) for job in jobs))
        for docker_executable, docker_image in images:
            check_call([docker_executable, 'pull', docker_image])

    # Make build_artefacts writable from this process
    artefacts_dir = _ensure_writable_dir(recipe_root, 'build_artefacts')

    # Here we will store the results of building
    results = []

    # Do not download recipe assets in parallel, so there are no race conditions
    # Checksum tests will fail otherwise (because some runs will check against partially downloaded assets)
    # Of course, this is a PITA (but it is hard to impossible to trick conda-build into using different dirs per job)
    if safe or not op.isdir(op.join(artefacts_dir, 'src_cache')):
        results.append(_run_one_linux_job(jobs[0]))

    # Run
    results += multiprocessing.dummy.Pool(n_threads).map(_run_one_linux_job, jobs)

    # Summary report
    for job, retcode, logfile in results:
        print(job.name, retcode if retcode else 'OK', logfile)

    print('Done')


def list_linux_jobs(recipe_root):
    records = []
    for job in _collect_linux_jobs(recipe_root):
        records.append(job.name + ': ' +
                       ' '.join('%s="%s"' % (k, v) for k, v in job.envvars) +
                       ' ' + op.join(recipe_root, 'ci_support', 'run_docker_build.sh'))
    return '\n'.join(records)


if __name__ == '__main__':
    RECIPES_ROOT = op.expanduser('~/Proyectos/--work/loopbio/condas-and-dockers/conda')
    recipe_root = op.join(RECIPES_ROOT, 'ffmpeg-feedstock')
    recipe_root = op.join(RECIPES_ROOT, 'ffmpeg-nogpl-feedstock')
    recipe_root = op.join(RECIPES_ROOT, 'av-feedstock')
    recipe_root = op.join(RECIPES_ROOT, 'opencv-vanilla-feedstock')
    recipe_root = op.join(RECIPES_ROOT, 'tensorflow-feedstock')
    recipe_root = op.join(RECIPES_ROOT, 'tensorboard-feedstock')
    run_linux_local(recipe_root, n_threads=4, only=[])

# Unfortunately circleci CLI does not really cut it for these cases:
#  https://circleci.com/docs/2.0/local-jobs/

# TODO: docker command should also be honored when rendering the circle CI recipe

# A goal of this script is to only read the final circleci configs.
# Using conda-forge.yml or the like is disallowed.
# In this way we are a bit more isolated on how information flows into .circleci/config.yml

# To be fully correct we probably should also use {{ docker.command }}
# instead of hardcoding "docker" in circleci rendering. But I do not think it will ever matter.

# Note that although that is true at the moment of writing this function (20170809),
# we do not assume that all jobs share the same docker (executable, image) configuration.
# Also, using docker_executable when pulling images is a bit over the top.
# It can be only useful if we ever allow the docker executable to vary per job and
# then if different executables do something different at pulling an image,
# none of which is true at the time of writing the function. However, it should prove
# harmless too, so there it is.

# FIXME: it could be great
# TODO: we should call docker kill when killing the parent process
#  - it does not stops the docker container gracefully if the thread/process is killed
#  - when SIGTERM and the like are captured by a program (e.g. pycharm) and using signal
#  - we are constrained by whatever is generated
# Until then: docker kill $(docker ps -q)
