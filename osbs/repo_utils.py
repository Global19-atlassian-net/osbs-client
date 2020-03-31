"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""


from __future__ import print_function, absolute_import, unicode_literals

from osbs.exceptions import OsbsException, OsbsValidationException
from osbs.constants import REPO_CONFIG_FILE, ADDITIONAL_TAGS_FILE, REPO_CONTAINER_CONFIG
from osbs.utils.labels import Labels
from six import StringIO
from six.moves.configparser import ConfigParser
from pkg_resources import resource_stream
from textwrap import dedent

import codecs
import json
import jsonschema
import logging
import os
import re
import yaml


logger = logging.getLogger(__name__)


def read_yaml_from_file_path(file_path, schema):
    with open(file_path) as f:
        yaml_data = f.read()
    return read_yaml(yaml_data, schema)


def read_yaml(yaml_data, schema):
    """
    :param yaml_data: string, yaml content
    """
    try:
        resource = resource_stream('osbs', schema)
        schema = codecs.getreader('utf-8')(resource)
    except (IOError, TypeError):
        logger.error('unable to extract JSON schema, cannot validate')
        raise

    try:
        schema = json.load(schema)
    except ValueError:
        logger.error('unable to decode JSON schema, cannot validate')
        raise
    data = yaml.safe_load(yaml_data)
    validator = jsonschema.Draft4Validator(schema=schema)
    try:
        jsonschema.Draft4Validator.check_schema(schema)
        validator.validate(data)
    except jsonschema.SchemaError:
        logger.error('invalid schema, cannot validate')
        raise
    except jsonschema.ValidationError:
        for error in validator.iter_errors(data):
            path = "".join(
                ('[{}]' if isinstance(element, int) else '.{}').format(element)
                for element in error.path
            )

            if path.startswith('.'):
                path = path[1:]

            logger.error('validation error (%s): %s', path or 'at top level', error.message)
        raise

    return data


class RepoInfo(object):
    """
    Aggregator for different aspects of the repository.
    """

    def __init__(self, dockerfile_parser=None, configuration=None, additional_tags=None):
        self.dockerfile_parser = dockerfile_parser
        self.configuration = configuration or RepoConfiguration()
        self.additional_tags = additional_tags or AdditionalTagsConfig(
            tags=self.configuration.container.get('tags', set()))
        self._parsed = False
        self._base_image = None
        self._labels = None

    @property
    def git_branch(self):
        return self.configuration.git_branch

    @property
    def git_ref(self):
        return self.configuration.git_ref

    @property
    def git_uri(self):
        return self.configuration.git_uri

    @property
    def git_commit_depth(self):
        return self.configuration.depth

    def _ensure_parsed(self):
        """Parse the Dockerfile and set self._labels and self._base_image."""

        if self._parsed:
            return

        self._parsed = True

        if self.configuration.is_flatpak:
            modules = self.configuration.container_module_specs

            if modules:
                module = modules[0]
            else:
                raise OsbsValidationException('"compose" config is missing "modules",'
                                              ' required for Flatpak')

            # modules is always required for a Flatpak build, but is only used
            # for the name and component labels if they aren't explicitly set
            # in container.yaml
            name = self.configuration.flatpak_name or module.name
            component = self.configuration.flatpak_component or module.name

            self._labels = Labels({
                Labels.LABEL_TYPE_NAME: name,
                Labels.LABEL_TYPE_COMPONENT: component,
                Labels.LABEL_TYPE_VERSION: module.stream,
            })

            self._base_image = self.configuration.flatpak_base_image
        else:
            df_parser = self.dockerfile_parser

            # DockerfileParse does not ensure a Dockerfile exists during initialization
            try:
                self._labels = Labels(df_parser.labels)
                self._base_image = df_parser.baseimage
            except IOError as e:
                raise RuntimeError('Could not parse Dockerfile in {}: {}'
                                   .format(df_parser.dockerfile_path, e))

    @property
    def labels(self):
        self._ensure_parsed()

        return self._labels

    @property
    def base_image(self):
        self._ensure_parsed()

        return self._base_image


class RepoConfiguration(object):
    """
    Read configuration from repository.
    """

    DEFAULT_CONFIG = dedent("""\
        [autorebuild]
        enabled = false
        """)

    def __init__(self, dir_path='', file_name=REPO_CONFIG_FILE, depth=None,
                 git_uri=None, git_branch=None, git_ref=None):
        self._config_parser = ConfigParser()
        self.container = {}
        self.depth = depth or 0
        self.autorebuild = {}
        # Keep track of the repo metadata in the repo configuration
        self.git_uri = git_uri
        self.git_branch = git_branch
        self.git_ref = git_ref

        # Set default options
        self._config_parser.readfp(StringIO(self.DEFAULT_CONFIG))   # pylint: disable=W1505; py2

        config_path = os.path.join(dir_path, file_name)
        if os.path.exists(config_path):
            self._config_parser.read(config_path)

        file_path = os.path.join(dir_path, REPO_CONTAINER_CONFIG)
        if os.path.exists(file_path):
            try:
                self.container = read_yaml_from_file_path(file_path, 'schemas/container.json') or {}
            except Exception as e:
                msg = ('Failed to load or validate container file "{file}": {reason}'
                       .format(file=file_path, reason=e))
                raise OsbsException(msg)

        # container values may be set to None
        container_compose = self.container.get('compose') or {}
        modules = container_compose.get('modules') or []

        self.autorebuild = self.container.get('autorebuild') or {}

        self.container_module_specs = []
        value_errors = []
        for module in modules:
            try:
                self.container_module_specs.append(ModuleSpec.from_str(module))
            except ValueError as e:
                value_errors.append(e)
        if value_errors:
            raise ValueError(value_errors)

        flatpak = self.container.get('flatpak') or {}
        self.is_flatpak = bool(flatpak)
        self.flatpak_base_image = flatpak.get('base_image')
        self.flatpak_component = flatpak.get('component')
        self.flatpak_name = flatpak.get('name')

    def is_autorebuild_enabled(self):
        return self._config_parser.getboolean('autorebuild', 'enabled')


class ModuleSpec(object):
    """
    Specification for a to-be-requested module.

    This module representation is simplified from the possible
    NAME:STREAM:VERSION:CONTEXT:ARCH/PROFILE by not supporting ARCH, which
    should be determined by the architecture of the build, and by not
    supporting partal specifications such as NAME:::CONTEXT.
    """

    def __init__(self, name, stream, version=None, context=None, profile=None):
        self.name = name
        self.stream = stream
        self.version = version
        self.context = context
        self.profile = profile

    def to_str(self, include_profile=True):
        result = self.name + ':' + self.stream
        if self.version:
            result += ':' + self.version
        if self.context:
            result += ':' + self.context
        if include_profile and self.profile:
            result += '/' + self.profile

        return result

    def __repr__(self):
        return "ModuleSpec({})".format(self.to_str())

    def __eq__(self, other):
        return self.__dict__ == other.__dict__

    __hash__ = None     # py2 compatibility

    @classmethod
    def from_str(cls, text):
        profile = None
        if '/' in text:
            module, profile = text.rsplit('/', 1)
        else:
            module = text

        pieces = module.split(':')
        if not 1 < len(pieces) < 5:
            raise ValueError('Module specification {} should be in '
                             'NAME:STREAM[:VERSION[:CONTEXT]][/PROFILE] format'.format(module))
        if not all(pieces) or profile == '':
            raise ValueError('Module specification {} contains empty fields'.format(module))
        return cls(*pieces, profile=profile)


class AdditionalTagsConfig(object):
    """
    Container for additional image tags.
    Tags are passed to constructor or are read from repository.
    """

    VALID_TAG_REGEX = re.compile(r'^[\w.]{0,127}$')

    def __init__(self, dir_path='', file_name=ADDITIONAL_TAGS_FILE, tags=None):
        tags = tags or set()
        self._tags = set([x for x in tags if self._is_tag_valid(x)])
        self._from_container_yaml = True if tags else False
        self._file_path = os.path.join(dir_path, file_name)

        self._populate_tags()

    def _populate_tags(self):
        if self._from_container_yaml:
            logger.warning('Tags were read from container.yaml file. Additional tags'
                           ' are being ignored!')
            return

        if not os.path.exists(self._file_path):
            return

        with open(self._file_path) as f:
            for tag in f:
                tag = tag.strip()
                if not self._is_tag_valid(tag):
                    continue
                self._tags.add(tag)

    def _is_tag_valid(self, tag):
        if not tag:
            return False

        if not self.VALID_TAG_REGEX.match(tag):
            logger.warning('Invalid additional tag "%s", must match pattern %s',
                           tag, self.VALID_TAG_REGEX.pattern)
            return False

        return True

    @property
    def tags(self):
        return list(self._tags)

    @property
    def from_container_yaml(self):
        return self._from_container_yaml
