"""
**docker** module is build on top of **benchmark** module to provide logic to create and run docker images
that are preconfigured with a given automl framework, and that can be used to run a benchmark anywhere.
The docker image embeds a version of the automlbenchmark app so that tasks are later run in local mode inside docker,
providing the same parameters and features allowing to import config and export results through mounted folders.
"""
import logging
import os
import re

from .benchmark import Benchmark, Job
from .resources import config as rconfig
from .results import Scoreboard
from .utils import run_cmd


log = logging.getLogger(__name__)


class DockerBenchmark(Benchmark):
    """DockerBenchmark
    an extension of Benchmark to run benchmarks inside docker.
    """

    @staticmethod
    def docker_image_name(framework_def):
        di = framework_def.docker_image
        return "{author}/{image}:{tag}".format(
            author=di.author,
            image=di.image if di.image else framework_def.name.lower(),
            tag=di.tag
        )

    def __init__(self, framework_name, benchmark_name, parallel_jobs=1):
        """

        :param framework_name:
        :param benchmark_name:
        :param parallel_jobs:
        """
        super().__init__(framework_name, benchmark_name, parallel_jobs)

    def _validate(self):
        if self.parallel_jobs == 0 or self.parallel_jobs > rconfig().max_parallel_jobs:
            log.warning("forcing parallelization to its upper limit: %s", rconfig().max_parallel_jobs)
            self.parallel_jobs = rconfig().max_parallel_jobs

    def setup(self, mode, upload=False):
        if mode == Benchmark.SetupMode.skip:
            return

        if mode == Benchmark.SetupMode.auto and self._docker_image_exists():
            return

        custom_commands = self.framework_module.docker_commands() if hasattr(self.framework_module, 'docker_commands') else ""
        self._generate_docker_script(custom_commands)
        self._build_docker_image(cache=(mode != Benchmark.SetupMode.force))
        if upload:
            self._upload_docker_image()

    def cleanup(self):
        # todo: remove generated docker script? anything else?
        pass

    def run(self, save_scores=False):
        jobs = []
        if self.parallel_jobs == 1:
            jobs.append(self._make_job())
        else:
            jobs.extend(self._benchmark_jobs())
        results = self._run_jobs(jobs)
        log.debug("results from docker run (merged to other scores but not to global scores yet): %s", results)
        return self._process_results(results, save_scores=save_scores)

    def run_one(self, task_name: str, fold, save_scores=False):
        jobs = []
        if self.parallel_jobs == 1 and (fold is None or (isinstance(fold, list) and len(fold) > 1)):
            jobs.append(self._make_job(task_name, fold))
        else:
            task_def = self._get_task_def(task_name)
            jobs.extend(self._custom_task_jobs(task_def, fold))
        results = self._run_jobs(jobs)
        log.debug("results from docker run (merged to other scores but not to global scores yet): %s", results)
        return self._process_results(results, task_name=task_name, save_scores=save_scores)

    def _fold_job(self, task_def, fold: int):
        return self._make_job(task_def.name, [fold])

    def _make_job(self, task_name=None, folds=None):
        folds = [] if folds is None else [str(f) for f in folds]

        def _run():
            self._start_docker("{framework} {benchmark} {task_param} {folds_param}".format(
                framework=self.framework_name,
                benchmark=self.benchmark_name,
                task_param='' if task_name is None else ('-t '+task_name),
                folds_param='' if len(folds) == 0 else ' '.join(['-f']+folds)
            ))
            # todo: would be nice to reload generated scores and return them

        job = Job("docker_{}_{}_{}".format(task_name if task_name else self.benchmark_name, ':'.join(folds), self.framework_name))
        job._run = _run
        return job

    def _start_docker(self, script_params=""):
        in_dir = rconfig().input_dir
        out_dir = rconfig().output_dir
        cmd = "docker run -v {input}:/input -v {output}:/output --rm {image} {params} -i /input -o /output -s skip".format(
            input=in_dir,
            output=out_dir,
            image=self._docker_image_name,
            params=script_params
        )
        log.info("Starting docker: %s", cmd)
        log.info("Datasets are loaded by default from folder %s", in_dir)
        log.info("Generated files will be available in folder %s", out_dir)
        output = run_cmd(cmd)
        log.debug(output)

    @property
    def _docker_script(self):
        return os.path.join(self._framework_dir, 'Dockerfile')

    @property
    def _docker_image_name(self):
        return DockerBenchmark.docker_image_name(self.framework_def)

    def _docker_image_exists(self):
        output = run_cmd("docker images -q {image}".format(image=self._docker_image_name))
        log.debug("docker image id: %s", output)
        return re.match(r'^[0-9a-f]+$', output.strip())

    def _build_docker_image(self, cache=True):
        log.info("Building docker image %s", self._docker_image_name)
        output = run_cmd("docker build {options} -t {container} -f {script} .".format(
            options="" if cache else "--no-cache",
            container=self._docker_image_name,
            script=self._docker_script
        ))
        log.info("Successfully built docker image %s", self._docker_image_name)
        log.debug(output)

    def _upload_docker_image(self):
        log.info("Publishing docker image %s", self._docker_image_name)
        output = run_cmd("docker login && docker push {}".format(self._docker_image_name))
        log.info("Successfully published docker image %s", self._docker_image_name)
        log.debug(output)

    def _generate_docker_script(self, custom_commands):
        docker_content = """FROM ubuntu:18.04

RUN apt-get update
RUN apt-get install -y curl wget unzip git
RUN apt-get install -y python3 python3-pip python3-venv
RUN pip3 install --upgrade pip

# We create a virtual environment so that AutoML systems may use their preferred versions of 
# packages that we need to data pre- and postprocessing without breaking it.
ENV PIP /venvs/bench/bin/pip3
ENV PY /venvs/bench/bin/python3 -W ignore
ENV SPIP pip3
ENV SPY python3

RUN $SPY -m venv /venvs/bench
RUN $PIP install --upgrade pip

WORKDIR /bench
VOLUME /input
VOLUME /output

# Add the AutoML system except files listed in .dockerignore (could also use git clone directly?)
ADD . /bench/

RUN $PIP install --no-cache-dir -r requirements.txt --process-dependency-links
RUN $PIP install --no-cache-dir openml

{custom_commands}

# https://docs.docker.com/engine/reference/builder/#entrypoint
ENTRYPOINT ["/bin/bash", "-c", "$PY {script} $0 $*"]
CMD ["{framework}", "test"]

""".format(custom_commands=custom_commands,
           framework=self.framework_name,
           script=rconfig().script)

        with open(self._docker_script, 'w') as file:
            file.write(docker_content)

