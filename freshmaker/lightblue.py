# -*- coding: utf-8 -*-
# Copyright (c) 2017  Red Hat, Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# Written by Chenxiong Qi <cqi@redhat.com>
#            Jan Kaluza <jkaluza@redhat.com>
#            Ralph Bean <rbean@redhat.com>

import yaml
import json
import os
import re
import requests
import six
import dogpile.cache
from itertools import groupby

from six.moves import http_client
import concurrent.futures
from freshmaker import log, conf
from freshmaker.kojiservice import koji_service
from freshmaker.utils import sorted_by_nvr, clone_distgit_repo, temp_dir
import koji


class LightBlueError(Exception):
    """Base class representing errors from LightBlue server"""

    def __init__(self, status_code, error_response):
        """Initialize

        :param int status_code: repsonse status code
        :param str or dict error_response: response content returned from
            LightBlue server that contains error content. There are two types of
            error. A piece of HTML when error happens in system-wide, for example,
            requested resource does not exists (404), and internal server error (500).
            It could also be a JSON data when error happens while LightBlue handles
            request.
        """
        self._raw = error_response
        self._status_code = status_code

    def __repr__(self):
        return '<{} [{}]>'.format(self.__class__.__name__, self.status_code)

    @property
    def raw(self):
        return self._raw

    @property
    def status_code(self):
        return self._status_code


class LightBlueSystemError(LightBlueError):
    """LightBlue system error"""

    def _get_error_message(self):
        # Try getting the error code from JSON if returned.
        try:
            msg = ""
            json_data = json.loads(self.raw)
            if "errors" in json_data:
                for error in json_data["errors"]:
                    if "msg" not in error or "errorCode" not in error:
                        continue
                    msg += error["errorCode"] + ": " + error["msg"] + "\n"
            if msg:
                return msg
        except ValueError as e:
            log.exception(e)
        # If no JSON is returned, try to get the title of HTML page.
        buf = six.StringIO(self.raw)
        html = ''.join((line.strip('\n') for line in buf))
        match = re.search('<title>(.+)</title>', html)
        return match.groups()[0]

    def __str__(self):
        try:
            return self._get_error_message()
        except Exception as e:
            log.exception(e)
            raise


class LightBlueRequestError(LightBlueError):
    """LightBlue request error"""

    def __str__(self):
        return 'Error{} ({}):\n{}'.format(
            's' if len(self.raw['errors']) > 1 else '',
            len(self.raw['errors']),
            '\n'.join(('    {}'.format(err['msg'])
                      for err in self.raw['errors']))
        )


class KojiLookupError(ValueError):
    """ Koji lookup error """
    pass


class ContainerRepository(dict):
    """Represent a container repository"""

    @classmethod
    def create(cls, data):
        repo = cls()
        repo.update(data)
        return repo


class ContainerImage(dict):
    """Represent a container image"""

    region = dogpile.cache.make_region().configure(conf.dogpile_cache_backend)

    @classmethod
    def create(cls, data):
        image = cls()
        image.update(data)
        return image

    def __hash__(self):
        return hash((self['brew']['build']))

    def log_error(self, err):
        """
        Logs the error associated with this image and sets self["error"].
        If there has been previous call of log_error, new `err` is appended
        to self['error'] with ';' separator.
        """
        prefix = ""
        if 'brew' in self and 'build' in self['brew']:
            prefix = self['brew']['build'] + ": "
        log.error("%s%s", prefix, err)
        if 'error' not in self or not self['error']:
            self['error'] = str(err)
        else:
            self['error'] += "; " + str(err)

    @property
    def is_base_image(self):
        return (self['parent'] is None and
                len(self['parsed_data']['layers']) == 2)

    @property
    def dockerfile(self):
        dockerfile = [file for file in self['parsed_data']['files']
                      if file['filename'] == 'Dockerfile']
        if not dockerfile:
            log.warning('Image %s does not contain a Dockerfile.',
                        self['brew']['build'])
            return None
        return dockerfile[0]

    def _get_default_additional_data(self):
        return {
            "repository": None,
            "commit": None,
            "target": None,
            "git_branch": None,
            "error": None,
            "arches": None,
        }

    @region.cache_on_arguments()
    def _get_additional_data_from_koji(self, nvr):
        """
        Finds the build defined by `nvr` in Koji and returns dict with
        additional information about this build including "repository",
        "commit", "target" and "git_branch".

        In case of lookup error, the "error" will be set to error string.
        """
        data = self._get_default_additional_data()

        with koji_service(
                conf.koji_profile, log, dry_run=conf.dry_run) as session:
            build = session.get_build(nvr)
            if not build:
                raise KojiLookupError(
                    "Cannot find Koji build with nvr %s in Koji" % nvr)

            if 'task_id' not in build or not build['task_id']:
                if ("extra" in build and
                        "container_koji_task_id" in build["extra"] and
                        build["extra"]["container_koji_task_id"]):
                    build['task_id'] = build["extra"]['container_koji_task_id']
                else:
                    raise KojiLookupError(
                        "Cannot find task_id or container_koji_task_id "
                        "in the Koji build %r" % build)

            brew_task = session.get_task_request(
                build['task_id'])
            source = brew_task[0]
            data["target"] = brew_task[1]
            extra_data = brew_task[2]
            if "git_branch" in extra_data:
                data["git_branch"] = extra_data["git_branch"]
            else:
                data["git_branch"] = "unknown"

            # Some builds do not have "source" attribute filled in, so try
            # both build["source"] and task_request[0] sources.
            sources = [source]
            if "source" in build:
                sources.insert(0, build["source"])
            for src in sources:
                m = re.match(r".*/(?P<namespace>.*)/(?P<container>.*)#(?P<commit>.*)", src)
                if m:
                    namespace = m.group("namespace")
                    # For some Koji tasks, the container part ends with "?" in
                    # source URL. This is just because some custom scripts for
                    # submitting those builds include this character in source URL
                    # to mark the query part of URL. We need to handle that by
                    # stripping that character.
                    container = m.group("container").rstrip("?")
                    data["repository"] = namespace + "/" + container

                    # There might be tasks which have branch name in
                    # "origin/branch_name" format, so detect it set commit
                    # hash only if this is not true.
                    if "/" not in m.group("commit"):
                        data["commit"] = m.group("commit")
                        break

            if not data['commit']:
                raise KojiLookupError(
                    "Cannot find valid source of Koji build %r" % build)

            data['arches'] = self._get_architectures_from_registry(build)

        return data

    def _get_architectures_from_registry(self, build):
        """ Determine the architectures of the build by reading the manifest """

        # First thing, check our feature flag.  This feature won't work if the OSBS instance we're
        # working with doesn't support supply arch_overrides, so, if our configuration says "don't
        # supply arch overrides to OSBS" then return None.
        if not conf.supply_arch_overrides:
            return None

        # If the image doesn't have the digest metadata we need, then we can
        # assume it is an old single-arch build.  But, if it does have a digest then carefully
        # query the registry for it to extract the list of arches produced last time.
        if 'extra' not in build:
            return 'x86_64'
        if 'image' not in build['extra']:
            return 'x86_64'
        if 'index' not in build['extra']['index']:
            return 'x86_64'

        index = build['extra']['image']['index']
        manifest_list = 'application/vnd.docker.distribution.manifest.list.v2+json'
        digest = index.get('digests', {}).get(manifest_list)

        if not digest:
            return 'x86_64'

        # If it has a digest, then it must have a pull url.
        registry_urls = [url for url in index['pull'] if digest in url]
        if not registry_urls:
            raise KojiLookupError(
                "Could not find pull url for Koji build %r %r" % (
                    build, digest))

        url = registry_urls[0].split(digest)[0].strip('@')
        response = requests.get(url, headers=dict(Accept=manifest_list))
        if not response.ok:
            raise KojiLookupError(
                "Could not pull manifest list from %s for %r: %r" % (
                    url, build, response))

        try:
            data = response.json()
        except ValueError as e:
            raise KojiLookupError(
                "Manifest list response for %r was not json: %r %s" % (
                    build, e, url))

        if 'manifests' not in data:
            raise KojiLookupError(
                "Manifest list response for %r was malformed: %s" % (
                    build, url))

        # Extract the list of arches, as written
        manifests = data['manifests']
        arches = [
            manifest['platform']['architecture']
            for manifest in manifests
            if 'platform' in manifest and 'architecture' in manifest['platform']
        ]
        # But!  Convert some arch values into ones familiar to Brew.
        # Notably, turn amd64 into x86_64.
        arches = [conf.manifest_v2_arch_map.get(arch, arch) for arch in arches]

        # Finally, return the list, joined.
        return ','.join(arches)

    @region.cache_on_arguments()
    def _get_additional_data_from_distgit(self, repository, branch, commit):
        """
        Finds out information about this image in distgit and returns a dict
        with following keys:

        - "generate_pulp_repos" - True when Freshmaker needs to generate Pulp
            repos using ODCS itself (it means it is not done by OSBS).
        - "content_sets" - List of x86_64 content_sets as defined in
            content_sets.yml. We care only about x86_64, because to build
            non-x86_64 image, OSBS will generate the Pulp repos and therefore
            we don't need content_sets in Freshmaker.
        """
        nvr = self["brew"]["build"]
        data = {"generate_pulp_repos": False,
                "content_sets": []}

        if not repository or not branch or not commit:
            log.warn("%s: Cannot get additional data from distgit.", nvr)
            return data
        if "/" in repository:
            namespace, name = repository.split("/")
        else:
            namespace = "rpms"
            name = repository

        prefix = "freshmaker-%s-%s-%s" % (namespace, name, commit)
        with temp_dir(prefix=prefix) as repodir:
            clone_distgit_repo(namespace, name, repodir, commit=commit,
                               ssh=False, logger=log)

            content_sets_path = os.path.join(repodir, "content_sets.yml")
            if not os.path.exists(content_sets_path):
                log.debug("%s: Should generate Pulp repo, %s does not exist.",
                          nvr, content_sets_path)
                data["generate_pulp_repos"] = True
                return data

            try:
                with open(content_sets_path, 'r') as f:
                    content_sets_yaml = yaml.load(f)
            except Exception as err:
                log.exception(err)
                data["generate_pulp_repos"] = True
                return data

            if "x86_64" in content_sets_yaml:
                data["content_sets"] = content_sets_yaml["x86_64"]

            container_path = os.path.join(repodir, "container.yaml")
            if not os.path.exists(container_path):
                log.debug("%s: Should generate Pulp repo, %s does not exist.",
                          nvr, container_path)
                data["generate_pulp_repos"] = True
                return data

            with open(container_path, 'r') as f:
                container_yaml = yaml.load(f)

            if ("compose" not in container_yaml or
                    "pulp_repos" not in container_yaml["compose"] or
                    not container_yaml["compose"]["pulp_repos"]):
                log.debug("%s: Should generate Pulp repo, pulp_repos not "
                          "enabled in %s.", nvr, container_path)
                data["generate_pulp_repos"] = True
                return data
        return data

    def resolve_commit(self):
        """
        Uses the ContainerImage data to resolve the information about
        commit from which the Docker image has been built.

        Sets the "repository and "commit" keys/values if available.
        """
        # Find the additional data for Container build in Koji.
        nvr = self["brew"]["build"]
        try:
            data = self._get_additional_data_from_koji(nvr)
        except KojiLookupError as e:
            err = "Cannot get data from Koji for build %s: %s." % (nvr, e)
            log.error(err)
            data = self._get_default_additional_data()
            data["error"] = err

        self.update(data)

    def resolve_content_sets(self, lb_instance, children=None):
        """
        Find out the content_sets this image uses and store it as
        "content_sets" key in image.

        :param LightBlue lb_instance: LightBlue instance to use for additional
            queries.
        :param list children: List of children to take the content_sets from in
            case this container image is unpublished and therefore without
            "content_sets" set.
        """
        data = self._get_additional_data_from_distgit(
            self["repository"], self["git_branch"], self["commit"])
        self["generate_pulp_repos"] = data["generate_pulp_repos"]

        # ContainerImage now has content_sets field, so use it if available.
        if "content_sets" in self and self["content_sets"]:
            log.info("Container image %s uses following content sets: %r",
                     self["brew"]["build"], self["content_sets"])
            if "content_sets_source" not in self:
                self["content_sets_source"] = "lightblue_container_image"
            return

        # Prefer content_sets from content_sets.yml.
        if data["content_sets"]:
            self["content_sets"] = data["content_sets"]
            self["content_sets_source"] = "distgit"
            log.info("Container image %s uses following content sets: %r",
                     self["brew"]["build"], data["content_sets"])
            return

        # In case content_sets cannot be get from content_sets.yml and also
        # are not set directly in this ContainerImage, try to get them from
        # children image.
        self["content_sets_source"] = "child_image"
        if not children:
            log.warning("Container image %s does not have 'content_sets' set "
                        "in Lightblue and also does not have any children, "
                        "this is suspicious.", self["brew"]["build"])
            self.update({"content_sets": []})
            return

        for child in children:
            # The child['content_sets'] should be always set for children
            # passed here, but in case it is not, just try it.
            if "content_sets" not in child:
                child.resolve(lb_instance, None)
            if not child["content_sets"]:
                continue

            log.info("Container image %s does not have 'content-sets' set "
                     "in Lightblue. Using child image %s content_sets: %r",
                     self["brew"]["build"], child["brew"]["build"],
                     child["content_sets"])
            self.update({"content_sets": child["content_sets"]})
            return

        log.warning("Container image %s does not have 'content_sets' set "
                    "in Lightblue as well as its children, this "
                    "is suspicious.", self["brew"]["build"])
        self.update({"content_sets": []})

    def resolve(self, lb_instance, children=None):
        """
        Resolves the Container image - populates additional metadata by
        querying Koji and dist-git.

        Calls self.resolve_commit() and self.resolve_content_sets().
        """
        self.resolve_commit()
        self.resolve_content_sets(lb_instance, children)


class LightBlue(object):
    """Interface to query lightblue"""

    def __init__(self, server_url, cert, private_key,
                 verify_ssl=None,
                 entity_versions=None):
        """Initialize LightBlue instance

        :param str server_url: URL used to call LightBlue APIs. It is
            unnecessary to include path part, which will be handled
            automatically. For example, https://lightblue.example.com/.
        :param str cert: path to certificate file.
        :param str private_key: path to private key file.
        :param bool verify_ssl: whether to verify SSL over HTTP. Enabled by
            default.
        :param dict entity_versions: a mapping from entity to what version
            should be used to request data. If no such a mapping appear , it
            means the default version will be used. You should choose versions
            explicitly. If entity_versions is omitted entirely, default version
            will be used on each entity.
        """
        self.server_url = server_url.rstrip('/')
        self.api_root = '{}/rest/data'.format(self.server_url)
        if verify_ssl is None:
            self.verify_ssl = True
        else:
            self.verify_ssl = verify_ssl

        if not os.path.exists(cert):
            raise IOError('Certificate file {} does not exist.'.format(cert))
        else:
            self.cert = cert

        if not os.path.exists(private_key):
            raise IOError('Private key file {} does not exist.'.format(private_key))
        else:
            self.private_key = private_key

        self.entity_versions = entity_versions or {}

    def _get_entity_version(self, entity_name):
        """Lookup configured entity's version

        :param str entity_name: entity name to get its version.
        :return: version configured for the entity name. If there is no
            corresponding version, emtpy string is returned, which can be used
            to construct request URL directly that means to use default
            version.
        :rtype: str
        """
        return self.entity_versions.get(entity_name, '')

    def _make_request(self, entity, data):
        """Make request to lightblue"""

        entity_url = '{}/{}'.format(self.api_root, entity)
        response = requests.post(entity_url,
                                 data=json.dumps(data),
                                 verify=self.verify_ssl,
                                 cert=(self.cert, self.private_key),
                                 headers={'Content-Type': 'application/json'})
        self._raise_expcetion_if_errors_returned(response)
        return response.json()

    def _raise_expcetion_if_errors_returned(self, response):
        """Raise exception when response contains errors

        :param dict response: the response returned from LightBlue, which is
            actually the requests response object.
        :raises LightBlueSystemError or LightBlueRequestError: if response
            status code is not 200. Otherwise, just keep silient.
        """
        status_code = response.status_code

        if status_code == http_client.OK:
            return

        if status_code in (http_client.NOT_FOUND,
                           http_client.INTERNAL_SERVER_ERROR,
                           http_client.UNAUTHORIZED):
            raise LightBlueSystemError(status_code, response.content)

        raise LightBlueRequestError(status_code, response.json())

    def find_container_repositories(self, request):
        """Query via entity containerRepository

        :param dict request: a map containing complete query expression.
            This query will be sent to LightBlue in a POST request. Refer to
            https://jewzaam.gitbooks.io/lightblue-specifications/content/language_specification/query.html
            to know more detail about how to write a query.
        :return: a list of ContainerRepository objects
        :rtype: list
        """

        url = 'find/containerRepository/{}'.format(
            self._get_entity_version('containerRepository'))
        response = self._make_request(url, request)

        repos = []
        for repo_data in response['processed']:
            if "auto_rebuild_tags" not in repo_data:
                repo_data["auto_rebuild_tags"] = ["latest"]
            repo = ContainerRepository()
            repo.update(repo_data)
            repos.append(repo)
        return repos

    def find_container_images(self, request):
        """Query via entity containerImage

        :param dict request: a map containing complete query expression.
            This query will be sent to LightBlue in a POST request. Refer to
            https://jewzaam.gitbooks.io/lightblue-specifications/content/language_specification/query.html
            to know more detail about how to write a query.
        :return: a list of ContainerImage objects
        :rtype: list
        """

        url = 'find/containerImage/{}'.format(
            self._get_entity_version('containerImage'))
        response = self._make_request(url, request)

        images = []
        for image_data in response['processed']:
            image = ContainerImage()
            image.update(image_data)
            images.append(image)
        return images

    def _set_container_repository_filters(
            self, request, published=True, deprecated=False,
            release_category="Generally Available"):
        """
        Sets the additional filters to containerRepository request
        based on the self.published, self.deprecated and self.release_category
        attributes.
        :param bool published: whether to limit queries to published
            repositories
        :param bool deprecated: set to True to limit results to deprecated
            repositories
        :param str release_category: filter only repositories with specific
            release category (options: Deprecated, Generally Available, Beta, Tech Preview)
        """
        if published is not None:
            request["query"]["$and"].append({
                "field": "published",
                "op": "=",
                "rvalue": published
            })

        if deprecated is not None:
            request["query"]["$and"].append({
                "field": "deprecated",
                "op": "=",
                "rvalue": deprecated
            })

        if release_category:
            request["query"]["$and"].append({
                "field": "release_categories.*",
                "op": "=",
                "rvalue": release_category
            })

        return request

    def find_all_container_repositories(
            self, published=True, deprecated=False,
            release_category="Generally Available"):
        """
        Returns dict with repository name as key and ContainerRepository as
        value.

        :param bool published: whether to limit queries to published
            repositories
        :param bool deprecated: set to True to limit results to deprecated
            repositories
        :param str release_category: filter only repositories with specific
            release category (options: Deprecated, Generally Available, Beta,
            Tech Preview)
        :rtype: dict
        :return: Dict with repository name as key and ContainerRepository as
            value.
        """
        repo_request = {
            "objectType": "containerRepository",
            "query": {
                "$and": []  # filled by _set_container_repository_filters().
            },
            "projection": [
                {"field": "repository", "include": True},
                {"field": "auto_rebuild_tags", "include": True, "recursive": True},
            ]
        }
        repo_request = self._set_container_repository_filters(
            repo_request, published, deprecated, release_category)
        repositories = self.find_container_repositories(repo_request)
        return {r["repository"]: r for r in repositories}

    def _get_default_projection(self, srpm_names=None, include_rpms=True):
        """
        Returns the default projection list for containerImage objects.

        :param list srpm_names: When not None, defines the SRPM names which
            are returned in "rpm_manifest" field of containerImage.;
        :param include_rpms: When False, "rpm_manifest" is not returned at all.
        """
        projection = [
            {"field": "brew", "include": True, "recursive": True},
            {"field": "parsed_data.files", "include": True, "recursive": True},
            {"field": "parsed_data.layers.*", "include": True, "recursive": True},
            {"field": "repositories.*.published", "include": True, "recursive": True},
            {"field": "repositories.*.repository", "include": True, "recursive": True},
            {"field": "repositories.*.tags.*.name", "include": True, "recursive": True},
            {"field": "content_sets", "include": True, "recursive": True},
        ]
        if include_rpms:
            if srpm_names:
                projection += [
                    {"field": "rpm_manifest.*.rpms", "include": True, "recursive": True,
                     "match": {
                         "$or": [{
                             "field": "srpm_name",
                             "op": "=",
                             "rvalue": srpm_name
                         } for srpm_name in srpm_names]}}]
            else:
                projection += [
                    {"field": "rpm_manifest.*.rpms", "include": True, "recursive": True},
                    {"field": "rpm_manifest.*.rpms.*.srpm_name", "include": True, "recursive": True},
                ]
        return projection

    def find_images_with_included_srpms(
            self, content_sets, srpm_names, repositories, published=True):
        """Query lightblue and find containerImages in given
        containerRepositories. By default limit only to images which have been
        published to at least one repository and images which have latest tag.

        :param list content_sets: List of content_sets the image includes RPMs
            from.
        :param list srpm_names: list of srpm_name (source rpm name) to look for
        :param list repositories: List of repository names to look for.
        :param bool published: whether to limit queries to published
            repositories
        """
        auto_rebuild_tags = set()
        for repo in repositories.values():
            auto_rebuild_tags |= set(repo["auto_rebuild_tags"])

        image_request = {
            "objectType": "containerImage",
            "query": {
                "$and": [
                    {
                        "$or": [{
                            "field": "content_sets.*",
                            "op": "=",
                            "rvalue": r
                        } for r in content_sets]
                    },
                    {
                        "$or": [{
                            "field": "repositories.*.tags.*.name",
                            "op": "=",
                            "rvalue": tag
                        } for tag in auto_rebuild_tags]
                    },
                    {
                        "$or": [{
                            "field": "rpm_manifest.*.rpms.*.srpm_name",
                            "op": "=",
                            "rvalue": srpm_name
                        } for srpm_name in srpm_names]
                    },
                    {
                        "field": "parsed_data.files.*.key",
                        "op": "=",
                        "rvalue": "buildfile"
                    },
                ]
            },
            "projection": self._get_default_projection(srpm_names=srpm_names)
        }

        if published is not None:
            image_request["query"]["$and"].append(
                {
                    "field": "repositories.*.published",
                    "op": "=",
                    "rvalue": published
                })

        images = self.find_container_images(image_request)
        if not images:
            return images

        # The image_request returns container images which are in the
        # right repository and are latest in *some* repository. But we need
        # those images to be latest in one of the `repositories`. It is not
        # trivial to generate LB query like this, so filter this client-side
        # for now.
        new_images = []
        for image in images:
            for repository in image["repositories"]:
                if repository["repository"] not in repositories:
                    continue
                published_repo = repositories[repository["repository"]]
                tag_names = [tag["name"] for tag in repository["tags"]]
                for auto_rebuild_tag in published_repo["auto_rebuild_tags"]:
                    if auto_rebuild_tag in tag_names:
                        new_images.append(image)
                        break
        images = new_images
        return images

    def find_unpublished_image_for_build(self, build):
        """
        Returns the unpublished variant of Docker image specified by `build`
        Brew build N-V-R.

        :param str build: Brew build N-V-R.
        :return: Unpublished container image.
        :rtype: ContainerImage.
        """
        image_request = {
            "objectType": "containerImage",
            "query": {
                "$and": [
                    {
                        "field": "brew.build",
                        "op": "=",
                        "rvalue": build
                    },
                    {
                        "$or": [
                            {
                                "field": "repositories.*.published",
                                "op": "=",
                                "rvalue": False
                            },
                            {
                                "field": "repositories#",
                                "op": "=",
                                "rvalue": 0
                            }
                        ]
                    }
                ]
            },
            "projection": self._get_default_projection(include_rpms=False)
        }
        images = self.find_container_images(image_request)
        if not images:
            return None
        return images[0]

    def get_image_by_layer(self, top_layer, build_layers_count,
                           srpm_name=None):
        """
        Find parent image by layer from either published repository or not

        :param str top_layer: the hash string representing an built image,
            which is usually the top layer in ``parsed_data.layers`` list.
        :param int build_layers_count: the number of build layers an image has.
        :param str srpm_name: name of the package. it is optional. if
            specified, will find image that also contains this package.

        :return: parent ContainerImage object. None is returned if no image is
            found.
        :rtype: ContainerImage
        """
        query = {
            "objectType": "containerImage",
            "query": {
                "$and": [
                    {
                        "field": "parsed_data.layers#",
                        "op": "$eq",
                        "rvalue": build_layers_count
                    },
                    {
                        "field": "parsed_data.layers.*",
                        "op": "$eq",
                        "rvalue": top_layer
                    },
                ],
            },
            "projection": self._get_default_projection(
                srpm_names=[srpm_name] if srpm_name else None,
                include_rpms=srpm_name is not None)
        }

        images = self.find_container_images(query)
        if not images:
            return None

        # Filter out images which do not contain srpm_name locally, because
        # filtering in lightblue takes long time and can even timeout
        # server-side.
        # We expect just at max 2 images here, published and unpublished, so
        # it is not big deal doing so.
        if srpm_name:
            tmp = []
            for image in images:
                if "rpm_manifest" not in image or not image["rpm_manifest"]:
                    continue
                # There can be just single "rpm_manifest". Lightblue returns
                # this as a list, because it is reference to
                # containerImageRPMManifest.
                rpm_manifest = image["rpm_manifest"][0]
                if "rpms" not in rpm_manifest:
                    continue
                rpms = rpm_manifest["rpms"]
                for rpm in rpms:
                    if "srpm_name" in rpm and rpm["srpm_name"] == srpm_name:
                        tmp.append(image)
                        break
            images = tmp
            if not images:
                return None

        for image in images:
            # we should prefer published image
            if 'repositories' in image:
                for repository in image['repositories']:
                    if repository['published']:
                        return image

        return images[0]

    def find_parent_images_with_package(self, child_image, srpm_name, layers):
        """
        Returns the chain of all parent images of the image with
        parsed_data.layers `layers` which contain the package `srpm_name`
        in their RPM manifest.

        The first item in the list is direct parent of the image in question.
        The last item in the list is the top level parent of the image in
        question.

        Docker images are layered and those layers are identified by its
        checksum in the ContainerImage["parsed_data"]["layers"] list.
        The first layer defined there is the layer defining the image
        itself, the second layer is the layer defining its parent, and so on.

        To find the parent image P of image X, we therefore have to search for
        an image which has P.parsed_data.layers[0] equal to
        X.parsed_data.layers[1]. However, query like this is not possible, so
        we search for any image containing the layer X.parsed_data.layers[1],
        but further limit the query to return only image which have the count
        of the layers equal to `build_layers_count`. For example, layers of an
        image

        [
           "sha256:3341bdf...b8e36168", <- layer of this image
           "sha256:5fc16d0...0e4e587e", <- probably the first parent image A
           "sha256:5d181d2...e6ad6992",
           "sha256:274f5cd...ff8fd6e7", <- parent image of parent image A
           "sha256:3ca89ba...b0ecae0e",
           "sha256:77ed333...a44a147a",
           "sha256:e2ec004...4c1fc873"
        ]

        Parent images will be retrieved though these layers from top to bottom.
        """
        images = []

        for idx, parent_top_layer in enumerate(layers[1:]):
            # `len(layers) - 1 - idx`. We decrement 1, because we skip the
            # first layer in for loop.
            parent_build_layers_count = len(layers) - 1 - idx
            image = self.get_image_by_layer(parent_top_layer,
                                            parent_build_layers_count,
                                            srpm_name=srpm_name)
            children = images if images else [child_image]
            if image:
                image.resolve(self, children)

            if images:
                if image:
                    images[-1]['parent'] = image
                else:
                    # If we did not find the parent image with the package,
                    # We still want to set the parent of the last image with
                    # the package so we know against which image it has been
                    # built.
                    parent = self.get_image_by_layer(parent_top_layer,
                                                     parent_build_layers_count)

                    children_image_layers_count = parent_build_layers_count + 1
                    if parent is None and children_image_layers_count != 2:
                        err = "Cannot find parent of image %s with layer %s " \
                            "and layer count %d in Lightblue, Lightblue data " \
                            "is probably incomplete" % (
                                children[-1]['brew']['build'], parent_top_layer,
                              parent_build_layers_count)
                        log.error(err)
                        if not images[-1]['error']:
                            images[-1]['error'] = err

                    if parent:
                        parent.resolve(self, images)
                    images[-1]['parent'] = parent
            if not image:
                return images
            images.append(image)

    def find_images_with_packages_from_content_set(
            self, srpm_names, content_sets, filter_fnc=None,
            published=True, deprecated=False,
            release_category="Generally Available"):
        """Query lightblue and find containers which contain given
        package from one of content sets

        :param list srpm_names: list of srpm_name (source rpm name) to look for
        :param list content_sets: list of strings (content sets) to consider
            when looking for the packages
        :param function filter_fnc: Function called as
            filter_fnc(container_image) with container_image being
            ContainerImage instance. If this function returns True, the image
            will not be considered for a rebuild as well as its parent images.
            This function is used to filter out images not allowed by
            Freshmaker configuration.
        :param bool published: whether to limit queries to published
            repositories
        :param bool deprecated: set to True to limit results to deprecated
            repositories
        :param str release_category: filter only repositories with specific
            release category (options: Deprecated, Generally Available, Beta, Tech Preview)

        :return: a list of dictionaries with three keys - repository, commit and
            srpm_nevra. Repository is a name git repository including the
            namespace. Commit is a git ref - usually a git commit
            hash. srpm_nevra is whole NEVRA of source rpm that is included in
            the given image - can be used for comparisons if needed
        :rtype: list
        """
        repos = self.find_all_container_repositories(
            published, deprecated, release_category)
        if not repos:
            return []
        images = self.find_images_with_included_srpms(
            content_sets, srpm_names, repos, published)

        # There can be multi-arch images which share the same
        # image['brew']['build']. Freshmaker is not interested in the image
        # architecture, it is only interested in NVR, so group the images
        # by the same image['brew']['build'] and include just first one in the
        # image list.
        sorted_images = sorted_by_nvr(
            images, get_nvr=lambda image: image['brew']['build'], reverse=True)
        images = []
        for k, v in groupby(sorted_images, key=lambda x: x['brew']['build']):
            images.append(v.next())

        # In case we query for unpublished images, we need to return just
        # the latest NVR for given name-version, otherwise images would
        # contain all the versions which ever containing the srpm_name.
        if not published:
            # Sort images by brew build NVR descending
            sorted_images = sorted_by_nvr(
                images, get_nvr=lambda image: image['brew']['build'], reverse=True)

            # Iterate over all the images and only keep the very first one
            # with the given name-version - this is the latest one.
            images = []
            seen_name_versions = []
            for image in sorted_images:
                parsed_build = koji.parse_NVR(image["brew"]["build"])
                nv = "%s-%s" % (parsed_build["name"], parsed_build["version"])
                if nv not in seen_name_versions:
                    images.append(image)
                    seen_name_versions.append(nv)

        # Filter out images based on the filter_fnc.
        if filter_fnc:
            images = [image for image in images if not filter_fnc(image)]

        for image in images:
            # We do not set "children" here in resolve_content_sets call, because
            # published images should have the content_set set.
            image.resolve(self, None)
            # Images returned by this method are latest released images, so
            # mark them like that.
            image["latest_released"] = True
        return images

    def _deduplicate_images_to_rebuild(self, to_rebuild):
        """
        Deduplicates the images to rebuild in `to_rebuild` in-place.

        The `to_rebuild` list is a list in following format:
            [
                [child_image, parent_of_child_image, parent_of_parent, ...],
                ...
            ]

        This methods goes through all the images in `to_rebuild` list and
        changes the list in a way that only single image with the highest
        release will exist for the given image name-version.

        For example, if there are three images in a list - foo-1-2, foo-1-3
        and foo-2-2, the foo-1-3 will be used instead of foo-1-2 on every
        occurence in a list, because the NVR is higher than NVR of foo-1-2.
        The foo-2-2 will be kept unchanged in a list, because it is the
        single record for the foo image in version 2.
        """
        # Temporary dict mapping the NVR of image to coordinates in the
        # `to_rebuild` list. For example
        # nvr_to_coordinates["nvr"] = [[0, 3], ...] means that the image with
        # nvr "nvr" is 4th image in the to_rebuild[0] list, ...
        nvr_to_coordinates = {}
        # Temporary dict mapping the NV to list of NVRs. The List of NVRs
        # is always sorted descending.
        nv_to_nvrs = {}
        # Temporary dict mapping the NVR to image.
        nvr_to_image = {}
        # Temporary dict mapping NV to latest released NVR for that NV.
        nv_to_latest_released_nvr = {}

        # Constructs the temporary dicts as desribed above.
        for image_id, images in enumerate(to_rebuild):
            for parent_id, image in enumerate(images):
                nvr = image["brew"]["build"]
                parsed_nvr = koji.parse_NVR(nvr)
                nv = "%s-%s" % (parsed_nvr["name"], parsed_nvr["version"])
                if nv not in nv_to_nvrs:
                    nv_to_nvrs[nv] = []
                if nvr not in nv_to_nvrs[nv]:
                    nv_to_nvrs[nv].append(nvr)
                if nvr not in nvr_to_coordinates:
                    nvr_to_coordinates[nvr] = []
                nvr_to_coordinates[nvr].append([image_id, parent_id])
                nvr_to_image[nvr] = image
                if "latest_released" in image and image["latest_released"]:
                    nv_to_latest_released_nvr[nv] = nvr

        # Sort the lists in nv_to_nvrs dict.
        for nv in nv_to_nvrs.keys():
            nv_to_nvrs[nv] = sorted_by_nvr(nv_to_nvrs[nv], reverse=True)

            # There might be container image NVRs which are not released yet,
            # but some released image is already built on top of them.
            # The issue is that such unreleased container image won't be in
            # its containerRepository and therefore won't have proper
            # content_sets set.
            # In this case, we copy the content_sets from the released image.
            # This might bring issue in case the content_sets changed
            # dramaticaly between released and unreleased release of such
            # image, but it's still the best guess we can do.
            # This is also used only as fallback in case "content_sets.yml"
            # does not exists in the dist-git repo, which should be rare
            # situation.
            latest_content_sets = []
            for nvr in reversed(nv_to_nvrs[nv]):
                image = nvr_to_image[nvr]
                if ("content_sets" not in image or
                        not image["content_sets"] or
                        "content_sets_source" not in image or
                        image["content_sets_source"] == "child_image"):
                    image["content_sets"] = latest_content_sets
                else:
                    latest_content_sets = image["content_sets"]

        # Iterate through list of NVs.
        for nv, nvrs in nv_to_nvrs.items():
            # We want to replace NVRs which are lower than the latest released
            # NVR with latest released NVR. If there are some higher NVRs, we
            # want to keep them, because we don't want to rebuild the image
            # against older NVR than the one it is currently built against.
            if nv in nv_to_latest_released_nvr:
                latest_released_nvr = nv_to_latest_released_nvr[nv]
            else:
                latest_released_nvr = nvrs[0]
            # The latest_released_nvr_index points to the latest released NVR
            # in the `nvrs` list. Because `nvrs` list is desc sorted, every NVR
            # with higher index is lower and therefore we need to replace it.
            latest_released_nvr_index = nvrs.index(latest_released_nvr)
            for nvr in nvrs[latest_released_nvr_index + 1:]:
                for image_id, parent_id in nvr_to_coordinates[nvr]:
                    # At first replace the image in to_rebuid based
                    # on the coordinates from temp dict.
                    to_rebuild[image_id][parent_id] = nvr_to_image[latest_released_nvr]

                    # And in case this image is not the the leaf image, also replace
                    # the ["parent"] record for the child image to point to the image
                    # with highest NVR.
                    if parent_id != 0:
                        to_rebuild[image_id][parent_id - 1]["parent"] = nvr_to_image[latest_released_nvr]

        return to_rebuild

    def _images_to_rebuild_to_batches(self, to_rebuild):
        """
        Creates batches with images as defined by `find_images_to_rebuild`
        output from the `to_rebuild` list in following format:

            [
                [child_image, parent_of_child_image, parent_of_parent, ...],
                ...
            ]
        """
        # At first get the max length of list in to_rebuild list.
        max_len = 0
        for rebuild_list in to_rebuild:
            max_len = max(len(rebuild_list), max_len)

        # Now create the batches with images. We still might find duplicate
        # images in to_rebuild lists in two cases:
        #
        # 1) A depends on X and also B depends on X. The X then would be
        #    added to first batch twice. This is simple to fix by just
        #    adding same image to batch once.
        # 2) A depends on X and A is also standalone image to rebuild. In this
        #    case, A would be in the second batch, because A must be built
        #    before X, but it is also standalone image to be rebuilt, so it
        #    would appear also in the first batch.
        #    To fix this, we at first add images with the longest dependency
        #    chains, so A will be added to second batch. Once we try to add
        #    standalone version of A, we won't add it, because it already
        #    exists in some batch.
        #
        # Both of these cases are handled by adding the image to `seen` set
        # and checking if it exists there already before adding it again.
        batches = [[] for i in range(max_len)]
        seen = set()
        for image_rebuild_list in sorted(to_rebuild, key=lambda lst: len(lst), reverse=True):
            for image, batch in zip(reversed(image_rebuild_list), batches):
                image_key = image["brew"]["build"]
                if image_key in seen:
                    continue
                seen.add(image_key)
                batch.append(image)
        return batches

    def find_images_to_rebuild(
            self, srpm_names, content_sets, published=True, deprecated=False,
            release_category="Generally Available", filter_fnc=None):
        """
        Find images to rebuild through image build layers

        Returns the list of sub-lists in which each sub-list contains
        ContainerImage instances which can be built in parallel. Sub-list N+1
        contains images which depend on images from sub-list N, so building any
        image from N+1 must happen *after* all of the images from sub-list N
        have been rebuilt.

        :param list srpm_names: List of srpm_name (source rpm name) to look for
        :param list content_sets: list of strings (content sets) to consider
            when looking for the packages
        :param bool published: whether to limit queries to published
            repositories
        :param bool deprecated: set to True to limit results to deprecated
            repositories
        :param str release_category: filter only repositories with specific
            release category (options: Deprecated, Generally Available, Beta, Tech Preview)
        :param function filter_fnc: Function called as
            filter_fnc(container_image) with container_image being
            ContainerImage instance. If this function returns True, the image
            will not be considered for a rebuild as well as its parent images.
            This function is used to filter out images not allowed by
            Freshmaker configuration.
        """
        images = self.find_images_with_packages_from_content_set(
            srpm_names, content_sets, filter_fnc, published, deprecated,
            release_category)

        def _get_images_to_rebuild(image):
            """
            Find out parent images to rebuild, helper called from threadpool.
            """
            rebuild_list = {}  # per srpm-name rebuild list.
            for srpm_name in srpm_names:
                for rpm in image["rpm_manifest"][0]["rpms"]:
                    if rpm["srpm_name"] == srpm_name:
                        break
                else:
                    # This `srpm_name` is not in image.
                    continue

                unpublished = self.find_unpublished_image_for_build(
                    image['brew']['build'])
                if not unpublished:
                    image.log_error(
                        "Cannot find unpublished version of image, Lightblue "
                        "data is probably incomplete")
                    rebuild_list[srpm_name] = [image]
                    continue

                layers = unpublished["parsed_data"]["layers"]
                rebuild_list[srpm_name] = self.find_parent_images_with_package(
                    image, srpm_name, layers)
                if rebuild_list[srpm_name]:
                    image['parent'] = rebuild_list[srpm_name][0]
                else:
                    parent = self.get_image_by_layer(layers[1], len(layers) - 1)
                    if parent:
                        parent.resolve(self, [image])
                    elif len(layers) != 2:
                        image.log_error(
                            "Cannot find parent image with layer %s and layer "
                            "count %d in Lightblue, Lightblue data is probably "
                            "incomplete" % (layers[1], len(layers) - 1))
                    image['parent'] = parent
                rebuild_list[srpm_name].insert(0, image)
            return rebuild_list

        # For every image, find out all its parent images which contain the
        # srpm_name package and store these lists to to_rebuild.
        to_rebuild = []
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=conf.max_thread_workers) as executor:
            futures = {executor.submit(_get_images_to_rebuild, i): i
                       for i in images}
            concurrent.futures.wait(futures)
            for future in futures:
                rebuild_lists = future.result()
                for rebuild_list in rebuild_lists.values():
                    to_rebuild.append(rebuild_list)

        # The to_rebuild list now contains all the images which need to be
        # rebuilt, but there are lot of duplicates there.

        # At first remove duplicated images which share the same name and
        # version, but different release.
        to_rebuild = self._deduplicate_images_to_rebuild(to_rebuild)

        # Now generate batches from deduplicated list and return it.
        return self._images_to_rebuild_to_batches(to_rebuild)
