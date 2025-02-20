from __future__ import annotations

import logging
import re
from abc import abstractmethod
from typing import Iterable, Optional

from jsonpath_ng import parse
from prometheus_client.core import GaugeMetricFamily

import pelorus
from committime import CommitMetric, commit_metric_from_build
from pelorus.utils import get_nested


class AbstractCommitCollector(pelorus.AbstractPelorusExporter):
    """
    Base class for a CommitCollector.
    This class should be extended for the system which contains the commit information.
    """

    def __init__(
        self,
        kube_client,
        username,
        token,
        namespaces,
        apps,
        collector_name,
        timedate_format,
        git_api=None,
        tls_verify=None,
    ):
        """Constructor"""
        self._kube_client = kube_client
        self._username = username
        self._token = token
        self._namespaces = namespaces
        self._apps = apps
        self._git_api = git_api
        self._tls_verify = tls_verify
        self._commit_dict = {}
        self._timedate_format = timedate_format
        self._collector_name = collector_name
        logging.info("=====Using %s Collector=====" % (self._collector_name))

    def collect(self):
        commit_metric = GaugeMetricFamily(
            "commit_timestamp",
            "Commit timestamp",
            labels=["namespace", "app", "commit", "image_sha"],
        )
        commit_metrics = self.generate_metrics(self._namespaces)
        for my_metric in commit_metrics:
            logging.info(
                "Collected commit_timestamp{ namespace=%s, app=%s, commit=%s, image_sha=%s } %s"
                % (
                    my_metric.namespace,
                    my_metric.name,
                    my_metric.commit_hash,
                    my_metric.image_hash,
                    str(float(my_metric.commit_timestamp)),
                )
            )
            commit_metric.add_metric(
                [
                    my_metric.namespace,
                    my_metric.name,
                    my_metric.commit_hash,
                    my_metric.image_hash,
                ],
                my_metric.commit_timestamp,
            )
            yield commit_metric

    def generate_metrics(
        self, watched_namespaces: Optional[str] = None
    ) -> Iterable[CommitMetric]:
        """Method called by the collect to create a list of metrics to publish"""
        # This will loop and look at OCP builds (calls get_git_commit_time)

        if not watched_namespaces:
            logging.info("No namespaces specified, watching all namespaces")
            v1_namespaces = self._kube_client.resources.get(
                api_version="v1", kind="Namespace"
            )
            watched_namespaces = [
                namespace.metadata.name for namespace in v1_namespaces.get().items
            ]
        logging.info("Watching namespaces: %s" % (watched_namespaces))

        # Initialize metrics list
        metrics = []
        for namespace in watched_namespaces:
            # Initialized variables
            builds = []
            apps = []
            builds_by_app = {}
            app_label = pelorus.get_app_label()
            logging.debug(
                "Searching for builds with label: %s in namespace: %s"
                % (app_label, namespace)
            )

            v1_builds = self._kube_client.resources.get(
                api_version="build.openshift.io/v1", kind="Build"
            )
            # only use builds that have the app label
            builds = v1_builds.get(namespace=namespace, label_selector=app_label)

            # use a jsonpath expression to find all values for the app label
            jsonpath_str = (
                "$['items'][*]['metadata']['labels']['" + str(app_label) + "']"
            )
            jsonpath_expr = parse(jsonpath_str)

            found = jsonpath_expr.find(builds)

            apps = [match.value for match in found]

            if not apps:
                continue
            # remove duplicates
            apps = list(dict.fromkeys(apps))
            builds_by_app = {}

            for app in apps:
                builds_by_app[app] = list(
                    filter(lambda b: b.metadata.labels[app_label] == app, builds.items)
                )

            metrics += self.get_metrics_from_apps(builds_by_app, namespace)

        return metrics

    @abstractmethod
    def get_commit_time(self, metric) -> CommitMetric:
        # This will perform the API calls and parse out the necessary fields into metrics
        pass

    def get_metrics_from_apps(self, apps, namespace):
        """Expects a sorted array of build data sorted by app label"""
        metrics = []
        for app in apps:

            builds = apps[app]
            jenkins_builds = list(
                filter(lambda b: b.spec.strategy.type == "JenkinsPipeline", builds)
            )
            code_builds = list(
                filter(
                    lambda b: b.spec.strategy.type in ["Source", "Binary", "Docker"],
                    builds,
                )
            )
            # assume for now that there will only be one repo/branch per app
            # For jenkins pipelines, we need to grab the repo data
            # then find associated s2i/docker builds from which to pull commit & image data
            repo_url = self.get_repo_from_jenkins(jenkins_builds)
            logging.debug("Repo URL for app %s is currently %s" % (app, repo_url))

            for build in code_builds:
                try:
                    metric = self.get_metric_from_build(build, app, namespace, repo_url)
                except Exception:
                    logging.error(
                        "Cannot collect metrics from build: %s" % (build.metadata.name)
                    )

                if metric:
                    logging.debug("Adding metric for app %s" % app)
                    metrics.append(metric)
        return metrics

    def get_metric_from_build(self, build, app, namespace, repo_url):
        errors = []
        try:
            metric = commit_metric_from_build(app, build, errors)

            if not self._is_metric_ready(namespace, metric, build):
                return None

            if repo_url:
                metric.repo_url = repo_url
            elif get_nested(build, "spec.source.git", default=None):
                metric.repo_url = get_nested(build, "spec.source.git.uri", name="build")
            else:
                metric.repo_url = self._get_repo_from_build_config(build)

            metric.labels = vars(build.metadata.labels)

            metric = self._get_commit_hash(metric, errors)

            if errors:
                msg = (
                    f"Missing data for CommitTime metric from Build "
                    f"{namespace}/{build.metadata.name} in app {app}: "
                    f"{'.'.join(str(e) for e in errors)}"
                )
                logging.warning(msg)
                return None

            return metric
        except AttributeError as e:
            # TODO: have we removed all the spots where we could get an AttributeError?
            logging.warning(
                "Build %s/%s in app %s is missing required attributes to collect data. Skipping.",
                namespace,
                build.metadata.name,
                app,
            )
            logging.debug(e, exc_info=True)
            return None
        except Exception as e:
            logging.error("Error encountered while getting CommitMetric info:")
            logging.error(e, exc_info=True)
            return None

    def _is_metric_ready(self, namespace: str, metric: CommitMetric, build) -> bool:
        """
        Determine if a build is ready to be examined.

        There's a few reasons we would stop early:
          - the build is new/pending/running and doesn't have an image yet.
          - the build failed/error'd/cancelled.
        These are valid conditions and we shouldn't clog the logs warning about it.
        However, if it's new/pending/running and _does_ have an image, we might as well continue.
        """
        build_status = get_nested(build, "status.phase", default=None)
        if build_status in {"Failed", "Error", "Cancelled"}:
            logging.debug(
                "Build %s/%s had status %s, skipping",
                namespace,
                build.metadata.name,
                build_status,
            )
            return False
        elif build_status in {"New, Pending", "Running"}:
            if metric.image_hash is None:
                logging.debug(
                    "Build %s/%s has status %s and doesn't have an image_hash yet, skipping",
                    namespace,
                    build.metadata.name,
                    build_status,
                )
                return False
            else:
                return True
        else:
            return True

    # TODO: be specific about the API modifying in place or returning a new metric.
    # Right now, it appears to do both.
    def _get_commit_hash(self, metric: CommitMetric, errors: list) -> CommitMetric:
        """
        Check the cache for the commit_time.
        If absent, call the API implemented by the subclass.
        """
        if metric.commit_hash not in self._commit_dict:
            logging.debug(
                "sha: %s, commit_timestamp not found in cache, executing API call.",
                metric.commit_hash,
            )
            metric = self.get_commit_time(metric)
            # If commit time is None, then we could not get the value from the API
            if metric.commit_time is None:
                errors.append("Couldn't get commit time")
            else:
                # Add the timestamp to the cache
                self._commit_dict[metric.commit_hash] = metric.commit_timestamp
        else:
            metric.commit_timestamp = self._commit_dict[metric.commit_hash]
            logging.debug(
                "Returning sha: %s, commit_timestamp: %s, from cache.",
                metric.commit_hash,
                metric.commit_timestamp,
            )

        return metric

    def get_repo_from_jenkins(self, jenkins_builds):
        if jenkins_builds:
            # First, check for cases where the source url is in pipeline params
            git_repo_regex = re.compile(
                r"((\w+://)|(.+@))([\w\d\.]+)(:[\d]+){0,1}/*(.*)"
            )
            for env in jenkins_builds[0].spec.strategy.jenkinsPipelineStrategy.env:
                logging.debug("Searching %s=%s for git urls" % (env.name, env.value))
                try:
                    result = git_repo_regex.match(env.value)
                except TypeError:
                    result = None
                if result:
                    logging.debug("Found result %s" % env.name)
                    return env.value

            try:
                # Then default to the repo listed in '.spec.source.git'
                return jenkins_builds[0].spec.source.git.uri
            except AttributeError:
                logging.debug(
                    "JenkinsPipelineStrategy build %s has no git repo configured. "
                    % jenkins_builds[0].metadata.name
                    + "Will check for source URLs in params."
                )
        # If no repo is found, we will return None, which will be handled later on

    def _get_repo_from_build_config(self, build):
        """
        Determines the repository url from the parent BuildConfig that created the Build resource in case
        the BuildConfig has the git uri but the Build does not
        :param build: the Build resource
        :return: repo_url as a str or None if not found
        """
        v1_build_configs = self._kube_client.resources.get(
            api_version="build.openshift.io/v1", kind="BuildConfig"
        )
        build_config = v1_build_configs.get(
            namespace=build.status.config.namespace, name=build.status.config.name
        )
        if build_config:
            if build_config.spec.source.git:
                git_uri = str(build_config.spec.source.git.uri)
                if git_uri.endswith(".git"):
                    return git_uri
                else:
                    return git_uri + ".git"

        return None
