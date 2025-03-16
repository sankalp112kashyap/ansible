# Copyright: (c) 2017, Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

import atexit
import decimal
import configparser
import os
import os.path
import sys
import stat
import tempfile

from collections import namedtuple
from collections.abc import Mapping, Sequence
from jinja2.nativetypes import NativeEnvironment

from ansible.errors import AnsibleOptionsError, AnsibleError, AnsibleRequiredOptionError
from ansible.module_utils.common.sentinel import Sentinel
from ansible.module_utils.common.text.converters import to_text, to_bytes, to_native
from ansible.module_utils.common.yaml import yaml_load
from ansible.module_utils.six import string_types
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.parsing.quoting import unquote
from ansible.parsing.yaml.objects import AnsibleVaultEncryptedUnicode
from ansible.utils.path import cleanup_tmp_file, makedirs_safe, unfrackpath


Setting = namedtuple('Setting', 'name value origin type')

INTERNAL_DEFS = {'lookup': ('_terms',)}

GALAXY_SERVER_DEF = [
    ('url', True, 'str'),
    ('username', False, 'str'),
    ('password', False, 'str'),
    ('token', False, 'str'),
    ('auth_url', False, 'str'),
    ('api_version', False, 'int'),
    ('validate_certs', False, 'bool'),
    ('client_id', False, 'str'),
    ('client_secret', False, 'str'),
    ('timeout', False, 'int'),
]

# config definition fields
GALAXY_SERVER_ADDITIONAL = {
    'api_version': {'default': None, 'choices': [2, 3]},
    'validate_certs': {'cli': [{'name': 'validate_certs'}]},
    'timeout': {'cli': [{'name': 'timeout'}]},
    'token': {'default': None},
}


def _get_entry(plugin_type, plugin_name, config):
    """ construct entry for requested config """
    entry = f'plugin_type: {plugin_type} ' if plugin_type else ''
    entry += f'plugin: {plugin_name} ' if plugin_name else ''
    entry += f'setting: {config} '
    return entry


# FIXME: see if we can unify in module_utils with similar function used by argspec
def ensure_type(value, value_type, origin=None, origin_ftype=None):
    """ return a configuration variable with casting """
    errmsg = ''
    basedir = origin if origin and os.path.isabs(origin) and os.path.exists(to_bytes(origin)) else None
    value_type = value_type.lower() if value_type else None

    if value is not None:
        try:
            if value_type in ('boolean', 'bool'):
                value = boolean(value, strict=False)
            elif value_type in ('integer', 'int'):
                value = int(decimal.Decimal(value))
            elif value_type == 'float':
                value = float(value)
            elif value_type == 'list':
                value = [unquote(x.strip()) for x in value.split(',')] if isinstance(value, string_types) else list(value)
            elif value_type == 'none':
                value = None if value == "None" else value
            elif value_type == 'path':
                value = resolve_path(value, basedir=basedir) if isinstance(value, string_types) else value
            elif value_type in ('tmp', 'temppath', 'tmppath'):
                value = resolve_path(value, basedir=basedir)
                if not os.path.exists(value):
                    makedirs_safe(value, 0o700)
                prefix = f'ansible-local-{os.getpid()}'
                value = tempfile.mkdtemp(prefix=prefix, dir=value)
                atexit.register(cleanup_tmp_file, value, warn=True)
            elif value_type == 'pathspec':
                value = [resolve_path(x, basedir=basedir) for x in value.split(os.pathsep)] if isinstance(value, string_types) else [resolve_path(x, basedir=basedir) for x in value]
            elif value_type == 'pathlist':
                value = [resolve_path(x, basedir=basedir) for x in value.split(',')] if isinstance(value, string_types) else [resolve_path(x, basedir=basedir) for x in value]
            elif value_type in ('dict', 'dictionary'):
                value = value if isinstance(value, Mapping) else value
            elif value_type in ('str', 'string'):
                value = to_text(value, errors='surrogate_or_strict')
                if origin_ftype == 'ini':
                    value = unquote(value)
            else:
                value = to_text(value, errors='surrogate_or_strict')
                if origin_ftype == 'ini':
                    value = unquote(value)
        except (decimal.DecimalException, ValueError) as e:
            errmsg = value_type
            raise ValueError(f'Invalid type provided for "{errmsg}": {value!r}') from e

    return to_text(value, errors='surrogate_or_strict', nonstring='passthru')


# FIXME: see if this can live in utils/path
def resolve_path(path, basedir=None):
    """ resolve relative or 'variable' paths """
    return unfrackpath(path.replace('{{CWD}}', os.getcwd()), follow=False, basedir=basedir)


# FIXME: generic file type?
def get_config_type(cfile):
    """ Determine the configuration file type based on its extension """
    if cfile:
        ext = os.path.splitext(cfile)[-1]
        if ext in ('.ini', '.cfg'):
            return 'ini'
        elif ext in ('.yaml', '.yml'):
            return 'yaml'
        else:
            raise AnsibleOptionsError(f"Unsupported configuration file extension for {cfile}: {to_native(ext)}")
    return None


# FIXME: can move to module_utils for use for ini plugins also?
def get_ini_config_value(p, entry):
    """ returns the value of last ini entry found """
    try:
        return p.get(entry.get('section', 'defaults'), entry.get('key', ''), raw=True)
    except Exception:
        return None


def find_ini_config_file(warnings=None):
    """ Load INI Config File order(first found is used): ENV, CWD, HOME, /etc/ansible """
    warnings = warnings or set()
    potential_paths = []

    path_from_env = os.getenv("ANSIBLE_CONFIG", Sentinel)
    if path_from_env is not Sentinel:
        path_from_env = unfrackpath(path_from_env, follow=False)
        if os.path.isdir(to_bytes(path_from_env)):
            path_from_env = os.path.join(path_from_env, "ansible.cfg")
        potential_paths.append(path_from_env)

    try:
        cwd = os.getcwd()
        perms = os.stat(cwd)
        cwd_cfg = os.path.join(cwd, "ansible.cfg")
        if perms.st_mode & stat.S_IWOTH and os.path.exists(cwd_cfg):
            warnings.add(f"Ansible is being run in a world writable directory ({cwd}), ignoring it as an ansible.cfg source. For more information see https://docs.ansible.com/ansible/devel/reference_appendices/config.html#cfg-in-world-writable-dir")
        else:
            potential_paths.append(to_text(cwd_cfg, errors='surrogate_or_strict'))
    except OSError:
        pass

    potential_paths.append(unfrackpath("~/.ansible.cfg", follow=False))
    potential_paths.append("/etc/ansible/ansible.cfg")

    for path in potential_paths:
        if os.path.exists(to_bytes(path)) and os.access(to_bytes(path), os.R_OK):
            return path
    return None


def _add_base_defs_deprecations(base_defs):
    """Add deprecation source 'ansible.builtin' to deprecations in base.yml"""
    def process(entry):
        if 'deprecated' in entry:
            entry['deprecated']['collection_name'] = 'ansible.builtin'

    for data in base_defs.values():
        process(data)
        for section in ('ini', 'env', 'vars'):
            if section in data:
                for entry in data[section]:
                    process(entry)


class ConfigManager(object):

    DEPRECATED = []  # type: list[tuple[str, dict[str, str]]]
    WARNINGS = set()  # type: set[str]

    def __init__(self, conf_file=None, defs_file=None):
        self._base_defs = {}
        self._plugins = {}
        self._parsers = {}
        self._config_file = conf_file

        self._base_defs = self._read_config_yaml_file(defs_file or f'{os.path.dirname(__file__)}/base.yml')
        _add_base_defs_deprecations(self._base_defs)

        if self._config_file is None:
            self._config_file = find_ini_config_file(self.WARNINGS)

        if self._config_file:
            self._parse_config_file()

        self._base_defs['CONFIG_FILE'] = {'default': None, 'type': 'path'}

    def load_galaxy_server_defs(self, server_list):
        def server_config_def(section, key, required, option_type):
            config_def = {
                'description': f'The {key} of the {section} Galaxy server',
                'ini': [{'section': f'galaxy_server.{section}', 'key': key}],
                'env': [{'name': f'ANSIBLE_GALAXY_SERVER_{section.upper()}_{key.upper()}'}],
                'required': required,
                'type': option_type,
            }
            if key in GALAXY_SERVER_ADDITIONAL:
                config_def.update(GALAXY_SERVER_ADDITIONAL[key])
                if key == 'timeout' and 'default' not in config_def:
                    config_def['default'] = self.get_config_value('GALAXY_SERVER_TIMEOUT')
            return config_def

        if server_list:
            for server_key in filter(None, server_list):
                defs = {k: server_config_def(server_key, k, req, value_type) for k, req, value_type in GALAXY_SERVER_DEF}
                self.initialize_plugin_configuration_definitions('galaxy_server', server_key, defs)

    def template_default(self, value, variables):
        if isinstance(value, string_types) and value.startswith('{{') and value.endswith('}}') and variables is not None:
            try:
                t = NativeEnvironment().from_string(value)
                value = t.render(variables)
            except Exception:
                pass
        return value

    def _read_config_yaml_file(self, yml_file):
        yml_file = to_bytes(yml_file)
        if os.path.exists(yml_file):
            with open(yml_file, 'rb') as config_def:
                return yaml_load(config_def) or {}
        raise AnsibleError(f"Missing base YAML definition file (bad install?): {to_native(yml_file)}")

    def _parse_config_file(self, cfile=None):
        if cfile is None:
            cfile = self._config_file

        ftype = get_config_type(cfile)
        if cfile and ftype == 'ini':
            self._parsers[cfile] = configparser.ConfigParser(inline_comment_prefixes=(';',))
            with open(to_bytes(cfile), 'rb') as f:
                try:
                    cfg_text = to_text(f.read(), errors='surrogate_or_strict')
                except UnicodeError as e:
                    raise AnsibleOptionsError(f"Error reading config file({cfile}) because the config file was not utf8 encoded: {to_native(e)}")
            try:
                self._parsers[cfile].read_string(cfg_text)
            except configparser.Error as e:
                raise AnsibleOptionsError(f"Error reading config file ({cfile}): {to_native(e)}")
        elif ftype:
            raise AnsibleOptionsError(f"Unsupported configuration file type: {to_native(ftype)}")

    def get_plugin_options(self, plugin_type, name, keys=None, variables=None, direct=None):
        defs = self.get_configuration_definitions(plugin_type=plugin_type, name=name)
        return {option: self.get_config_value(option, plugin_type=plugin_type, plugin_name=name, keys=keys, variables=variables, direct=direct) for option in defs}

    def get_plugin_vars(self, plugin_type, name):
        return [var_entry['name'] for pdef in self.get_configuration_definitions(plugin_type=plugin_type, name=name).values() if 'vars' in pdef for var_entry in pdef['vars']]

    def get_plugin_options_from_var(self, plugin_type, name, variable):
        return [option_name for option_name, pdef in self.get_configuration_definitions(plugin_type=plugin_type, name=name).items() if 'vars' in pdef for var_entry in pdef['vars'] if variable == var_entry['name']]

    def get_configuration_definition(self, name, plugin_type=None, plugin_name=None):
        if plugin_type is None:
            return self._base_defs.get(name, None)
        if plugin_name is None:
            return self._plugins.get(plugin_type, {}).get(name, None)
        return self._plugins.get(plugin_type, {}).get(plugin_name, {}).get(name, None)

    def has_configuration_definition(self, plugin_type, name):
        return name in self._plugins.get(plugin_type, {})

    def get_configuration_definitions(self, plugin_type=None, name=None, ignore_private=False):
        ret = self._base_defs if plugin_type is None else self._plugins.get(plugin_type, {}).get(name, {})
        if ignore_private:
            ret = {k: v for k, v in ret.items() if not k.startswith('_')}
        return ret

    def _loop_entries(self, container, entry_list):
        for entry in entry_list:
            name = entry.get('name')
            try:
                temp_value = container.get(name, None)
                if temp_value is not None:
                    if isinstance(temp_value, AnsibleVaultEncryptedUnicode):
                        temp_value = to_text(temp_value, errors='surrogate_or_strict')
                    if 'deprecated' in entry:
                        self.DEPRECATED.append((entry['name'], entry['deprecated']))
                    return temp_value, name
            except UnicodeEncodeError:
                self.WARNINGS.add(f'value for config entry {to_text(name)} contains invalid characters, ignoring...')
        return None, None

    def get_config_value(self, config, cfile=None, plugin_type=None, plugin_name=None, keys=None, variables=None, direct=None):
        try:
            value, _ = self.get_config_value_and_origin(config, cfile=cfile, plugin_type=plugin_type, plugin_name=plugin_name, keys=keys, variables=variables, direct=direct)
        except AnsibleError:
            raise
        except Exception as e:
            raise AnsibleError(f"Unhandled exception when retrieving {config}:\n{to_native(e)}", orig_exc=e)
        return value

    def get_config_value_and_origin(self, config, cfile=None, plugin_type=None, plugin_name=None, keys=None, variables=None, direct=None):
        if cfile is None:
            cfile = self._config_file

        if config == 'CONFIG_FILE':
            return cfile, ''

        value, origin, origin_ftype = None, None, None
        defs = self.get_configuration_definitions(plugin_type=plugin_type, name=plugin_name)
        if config in defs:
            aliases = defs[config].get('aliases', [])

            if direct:
                value = direct.get(config, next((direct[alias] for alias in aliases if alias in direct), None))
                origin = 'Direct' if value is not None else origin

            if value is None and variables and defs[config].get('vars'):
                value, origin = self._loop_entries(variables, defs[config]['vars'])
                origin = f'var: {origin}' if origin else origin

            if value is None and defs[config].get('keyword') and keys:
                value, origin = self._loop_entries(keys, defs[config]['keyword'])
                origin = f'keyword: {origin}' if origin else origin

            if value is None and keys:
                value = keys.get(config, next((keys[alias] for alias in aliases if alias in keys), None))
                origin = f'keyword: {config}' if value is not None else origin

            if value is None and 'cli' in defs[config]:
                from ansible import context
                value, origin = self._loop_entries(context.CLIARGS, defs[config]['cli'])
                origin = f'cli: {origin}' if origin else origin

            if value is None and defs[config].get('env'):
                value, origin = self._loop_entries(os.environ, defs[config]['env'])
                origin = f'env: {origin}' if origin else origin

            if value is None and cfile:
                if self._parsers.get(cfile) is None:
                    self._parse_config_file(cfile)
                ftype = get_config_type(cfile)
                if ftype and defs[config].get(ftype):
                    for entry in defs[config][ftype]:
                        temp_value = get_ini_config_value(self._parsers[cfile], entry) if ftype == 'ini' else None
                        if temp_value is not None:
                            value, origin, origin_ftype = temp_value, cfile, ftype
                            if 'deprecated' in entry and ftype == 'ini':
                                self.DEPRECATED.append((f'[{entry["section"]}]{entry["key"]}', entry['deprecated']))

            if value is None:
                if defs[config].get('required', False) and (not plugin_type or config not in INTERNAL_DEFS.get(plugin_type, {})):
                    raise AnsibleRequiredOptionError(f"No setting was provided for required configuration {to_native(_get_entry(plugin_type, plugin_name, config))}")
                value, origin = self.template_default(defs[config].get('default'), variables), 'default'

            try:
                value = ensure_type(value, defs[config].get('type'), origin=origin, origin_ftype=origin_ftype)
            except ValueError as e:
                if origin.startswith('env:') and value == '':
                    value = ensure_type(defs[config].get('default'), defs[config].get('type'), origin='default', origin_ftype=origin_ftype)
                else:
                    raise AnsibleOptionsError(f'Invalid type for configuration option {to_native(_get_entry(plugin_type, plugin_name, config)).strip()} (from {origin}): {to_native(e)}')

            if value is not None and 'choices' in defs[config] and defs[config]['choices'] is not None:
                valid_choices = defs[config]['choices']
                if defs[config].get('type') == 'list':
                    if not all(choice in valid_choices for choice in value):
                        raise AnsibleOptionsError(f'Invalid value "{value}" for configuration option "{to_native(_get_entry(plugin_type, plugin_name, config))}", valid values are: {valid_choices}')
                elif value not in valid_choices:
                    raise AnsibleOptionsError(f'Invalid value "{value}" for configuration option "{to_native(_get_entry(plugin_type, plugin_name, config))}", valid values are: {valid_choices}')

            if 'deprecated' in defs[config] and origin != 'default':
                self.DEPRECATED.append((config, defs[config].get('deprecated')))
        else:
            raise AnsibleError(f'Requested entry ({to_native(_get_entry(plugin_type, plugin_name, config))}) was not defined in configuration.')

        return value, origin

    def initialize_plugin_configuration_definitions(self, plugin_type, name, defs):
        self._plugins.setdefault(plugin_type, {})[name] = defs

    @staticmethod
    def get_deprecated_msg_from_config(dep_docs, include_removal=False, collection_name=None):
        removal = ''
        if include_removal:
            removal = f"Will be removed in a release after {dep_docs['removed_at_date']}\n\t" if 'removed_at_date' in dep_docs else f"Will be removed in: {collection_name} {dep_docs['removed_in']}\n\t" if collection_name else f"Will be removed in: Ansible {dep_docs['removed_in']}\n\t"
        alt = dep_docs.get('alternatives', dep_docs.get('alternative', 'none'))
        return f"Reason: {dep_docs['why']}\n\t{removal}Alternatives: {alt}"
