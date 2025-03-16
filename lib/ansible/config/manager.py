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
    entry = ''
    if plugin_type:
        entry += 'plugin_type: %s ' % plugin_type
        if plugin_name:
            entry += 'plugin: %s ' % plugin_name
    entry += 'setting: %s ' % config
    return entry


def ensure_type(value, value_type, origin=None, origin_ftype=None):
    """Return a configuration variable with correct typing"""
    if value is None or not value_type:
        return to_text(value, errors='surrogate_or_strict', nonstring='passthru')

    value_type = value_type.lower()
    basedir = origin if origin and os.path.isabs(origin) and os.path.exists(to_bytes(origin)) else None

    try:
        if value_type in ('boolean', 'bool'):
            return boolean(value, strict=False)
        elif value_type in ('integer', 'int'):
            if isinstance(value, int):
                return value
            decimal_value = decimal.Decimal(value)
            int_part = int(decimal_value)
            return int_part if decimal_value == int_part else None
        elif value_type == 'float':
            return float(value) if not isinstance(value, float) else value
        elif value_type == 'list':
            return [unquote(x.strip()) for x in value.split(',')] if isinstance(value, string_types) else value
        elif value_type == 'none':
            return None if value == "None" else value
        elif value_type == 'path':
            return resolve_path(value, basedir=basedir) if isinstance(value, string_types) else value
        elif value_type in ('tmp', 'temppath', 'tmppath'):
            if isinstance(value, string_types):
                path = resolve_path(value, basedir=basedir)
                if not os.path.exists(path):
                    makedirs_safe(path, 0o700)
                tmp_path = tempfile.mkdtemp(prefix=f'ansible-local-{os.getpid()}', dir=path)
                atexit.register(cleanup_tmp_file, tmp_path, warn=True)
                return tmp_path
        elif value_type in ('pathspec', 'pathlist'):
            if isinstance(value, string_types):
                paths = value.split(os.pathsep if value_type == 'pathspec' else ',')
                return [resolve_path(x.strip(), basedir=basedir) for x in paths]
            return [resolve_path(x, basedir=basedir) for x in value] if isinstance(value, Sequence) else value
        elif value_type in ('str', 'string', 'dict', 'dictionary'):
            if isinstance(value, (string_types, AnsibleVaultEncryptedUnicode, bool, int, float, complex)):
                text_value = to_text(value, errors='surrogate_or_strict')
                return unquote(text_value) if origin_ftype == 'ini' else text_value
            return value if value_type in ('dict', 'dictionary') else None
        
        # Default string handling
        if isinstance(value, (string_types, AnsibleVaultEncryptedUnicode)):
            text_value = to_text(value, errors='surrogate_or_strict')
            return unquote(text_value) if origin_ftype == 'ini' else text_value
        
        return value

    except (ValueError, decimal.DecimalException) as e:
        raise ValueError(f'Invalid type provided for "{value_type}": {value!r}') from e


def resolve_path(path, basedir=None):
    """ resolve relative or 'variable' paths """
    if '{{CWD}}' in path:  # allow users to force CWD using 'magic' {{CWD}}
        path = path.replace('{{CWD}}', os.getcwd())

    return unfrackpath(path, follow=False, basedir=basedir)


def get_config_type(cfile):

    ftype = None
    if cfile is not None:
        ext = os.path.splitext(cfile)[-1]
        if ext in ('.ini', '.cfg'):
            ftype = 'ini'
        elif ext in ('.yaml', '.yml'):
            ftype = 'yaml'
        else:
            raise AnsibleOptionsError("Unsupported configuration file extension for %s: %s" % (cfile, to_native(ext)))

    return ftype


def get_ini_config_value(p, entry):
    """ returns the value of last ini entry found """
    value = None
    if p is not None:
        try:
            value = p.get(entry.get('section', 'defaults'), entry.get('key', ''), raw=True)
        except Exception:  # FIXME: actually report issues here
            pass
    return value


def find_ini_config_file(warnings=None):
    """ Load INI Config File order(first found is used): ENV, CWD, HOME, /etc/ansible """
    # FIXME: eventually deprecate ini configs

    if warnings is None:
        # Note: In this case, warnings does nothing
        warnings = set()

    potential_paths = []

    # A value that can never be a valid path so that we can tell if ANSIBLE_CONFIG was set later
    # We can't use None because we could set path to None.
    # Environment setting
    path_from_env = os.getenv("ANSIBLE_CONFIG", Sentinel)
    if path_from_env is not Sentinel:
        path_from_env = unfrackpath(path_from_env, follow=False)
        if os.path.isdir(to_bytes(path_from_env)):
            path_from_env = os.path.join(path_from_env, "ansible.cfg")
        potential_paths.append(path_from_env)

    # Current working directory
    warn_cmd_public = False
    try:
        cwd = os.getcwd()
        perms = os.stat(cwd)
        cwd_cfg = os.path.join(cwd, "ansible.cfg")
        if perms.st_mode & stat.S_IWOTH:
            # Working directory is world writable so we'll skip it.
            # Still have to look for a file here, though, so that we know if we have to warn
            if os.path.exists(cwd_cfg):
                warn_cmd_public = True
        else:
            potential_paths.append(to_text(cwd_cfg, errors='surrogate_or_strict'))
    except OSError:
        # If we can't access cwd, we'll simply skip it as a possible config source
        pass

    # Per user location
    potential_paths.append(unfrackpath("~/.ansible.cfg", follow=False))

    # System location
    potential_paths.append("/etc/ansible/ansible.cfg")

    for path in potential_paths:
        b_path = to_bytes(path)
        if os.path.exists(b_path) and os.access(b_path, os.R_OK):
            break
    else:
        path = None

    # Emit a warning if all the following are true:
    # * We did not use a config from ANSIBLE_CONFIG
    # * There's an ansible.cfg in the current working directory that we skipped
    if path_from_env != path and warn_cmd_public:
        warnings.add(u"Ansible is being run in a world writable directory (%s),"
                     u" ignoring it as an ansible.cfg source."
                     u" For more information see"
                     u" https://docs.ansible.com/ansible/devel/reference_appendices/config.html#cfg-in-world-writable-dir"
                     % to_text(cwd))

    return path


def _add_base_defs_deprecations(base_defs):
    """Add deprecation source 'ansible.builtin' to deprecations in base.yml"""
    def process(entry):
        if 'deprecated' in entry:
            entry['deprecated']['collection_name'] = 'ansible.builtin'

    for dummy, data in base_defs.items():
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

        self._base_defs = self._read_config_yaml_file(defs_file or ('%s/base.yml' % os.path.dirname(__file__)))
        _add_base_defs_deprecations(self._base_defs)

        if self._config_file is None:
            # set config using ini
            self._config_file = find_ini_config_file(self.WARNINGS)

        # consume configuration
        if self._config_file:
            # initialize parser and read config
            self._parse_config_file()

        # ensure we always have config def entry
        self._base_defs['CONFIG_FILE'] = {'default': None, 'type': 'path'}

    def load_galaxy_server_defs(self, server_list):

        def server_config_def(section, key, required, option_type):
            config_def = {
                'description': 'The %s of the %s Galaxy server' % (key, section),
                'ini': [
                    {
                        'section': 'galaxy_server.%s' % section,
                        'key': key,
                    }
                ],
                'env': [
                    {'name': 'ANSIBLE_GALAXY_SERVER_%s_%s' % (section.upper(), key.upper())},
                ],
                'required': required,
                'type': option_type,
            }
            if key in GALAXY_SERVER_ADDITIONAL:
                config_def.update(GALAXY_SERVER_ADDITIONAL[key])
                # ensure we always have a default timeout
                if key == 'timeout' and 'default' not in config_def:
                    config_def['default'] = self.get_config_value('GALAXY_SERVER_TIMEOUT')

            return config_def

        if server_list:
            for server_key in server_list:
                if not server_key:
                    # To filter out empty strings or non truthy values as an empty server list env var is equal to [''].
                    continue

                # Config definitions are looked up dynamically based on the C.GALAXY_SERVER_LIST entry. We look up the
                # section [galaxy_server.<server>] for the values url, username, password, and token.
                defs = dict((k, server_config_def(server_key, k, req, value_type)) for k, req, value_type in GALAXY_SERVER_DEF)
                self.initialize_plugin_configuration_definitions('galaxy_server', server_key, defs)

    def template_default(self, value, variables):
        if isinstance(value, string_types) and (value.startswith('{{') and value.endswith('}}')) and variables is not None:
            # template default values if possible
            # NOTE: cannot use is_template due to circular dep
            try:
                t = NativeEnvironment().from_string(value)
                value = t.render(variables)
            except Exception:
                pass  # not templatable
        return value

    def _read_config_yaml_file(self, yml_file):
        # TODO: handle relative paths as relative to the directory containing the current playbook instead of CWD
        # Currently this is only used with absolute paths to the `ansible/config` directory
        yml_file = to_bytes(yml_file)
        if os.path.exists(yml_file):
            with open(yml_file, 'rb') as config_def:
                return yaml_load(config_def) or {}
        raise AnsibleError(
            "Missing base YAML definition file (bad install?): %s" % to_native(yml_file))

    def _parse_config_file(self, cfile=None):
        """ return flat configuration settings from file(s) """
        # TODO: take list of files with merge/nomerge

        if cfile is None:
            cfile = self._config_file

        ftype = get_config_type(cfile)
        if cfile is not None:
            if ftype == 'ini':
                self._parsers[cfile] = configparser.ConfigParser(inline_comment_prefixes=(';',))
                with open(to_bytes(cfile), 'rb') as f:
                    try:
                        cfg_text = to_text(f.read(), errors='surrogate_or_strict')
                    except UnicodeError as e:
                        raise AnsibleOptionsError("Error reading config file(%s) because the config file was not utf8 encoded: %s" % (cfile, to_native(e)))
                try:
                    self._parsers[cfile].read_string(cfg_text)
                except configparser.Error as e:
                    raise AnsibleOptionsError("Error reading config file (%s): %s" % (cfile, to_native(e)))
            # FIXME: this should eventually handle yaml config files
            # elif ftype == 'yaml':
            #     with open(cfile, 'rb') as config_stream:
            #         self._parsers[cfile] = yaml_load(config_stream)
            else:
                raise AnsibleOptionsError("Unsupported configuration file type: %s" % to_native(ftype))

    def _find_yaml_config_files(self):
        """ Load YAML Config Files in order, check merge flags, keep origin of settings"""
        pass

    def get_plugin_options(self, plugin_type, name, keys=None, variables=None, direct=None):

        options = {}
        defs = self.get_configuration_definitions(plugin_type=plugin_type, name=name)
        for option in defs:
            options[option] = self.get_config_value(option, plugin_type=plugin_type, plugin_name=name, keys=keys, variables=variables, direct=direct)

        return options

    def get_plugin_vars(self, plugin_type, name):

        pvars = []
        for pdef in self.get_configuration_definitions(plugin_type=plugin_type, name=name).values():
            if 'vars' in pdef and pdef['vars']:
                for var_entry in pdef['vars']:
                    pvars.append(var_entry['name'])
        return pvars

    def get_plugin_options_from_var(self, plugin_type, name, variable):

        options = []
        for option_name, pdef in self.get_configuration_definitions(plugin_type=plugin_type, name=name).items():
            if 'vars' in pdef and pdef['vars']:
                for var_entry in pdef['vars']:
                    if variable == var_entry['name']:
                        options.append(option_name)
        return options

    def get_configuration_definition(self, name, plugin_type=None, plugin_name=None):

        ret = {}
        if plugin_type is None:
            ret = self._base_defs.get(name, None)
        elif plugin_name is None:
            ret = self._plugins.get(plugin_type, {}).get(name, None)
        else:
            ret = self._plugins.get(plugin_type, {}).get(plugin_name, {}).get(name, None)

        return ret

    def has_configuration_definition(self, plugin_type, name):

        has = False
        if plugin_type in self._plugins:
            has = (name in self._plugins[plugin_type])

        return has

    def get_configuration_definitions(self, plugin_type=None, name=None, ignore_private=False):
        """ just list the possible settings, either base or for specific plugins or plugin """

        ret = {}
        if plugin_type is None:
            ret = self._base_defs
        elif name is None:
            ret = self._plugins.get(plugin_type, {})
        else:
            ret = self._plugins.get(plugin_type, {}).get(name, {})

        if ignore_private:
            for cdef in list(ret.keys()):
                if cdef.startswith('_'):
                    del ret[cdef]
        return ret

    def _loop_entries(self, container, entry_list):
        """Process config entries and handle deprecation"""
        for entry in entry_list:
            name = entry.get('name')
            try:
                value = container.get(name)
                if value is not None:
                    if isinstance(value, AnsibleVaultEncryptedUnicode):
                        value = to_text(value, errors='surrogate_or_strict')
                    if 'deprecated' in entry:
                        self.DEPRECATED.append((entry['name'], entry['deprecated']))
                    return value, name
            except UnicodeEncodeError:
                self.WARNINGS.add(f'value for config entry {to_text(name)} contains invalid characters, ignoring...')
        return None, None

    def get_config_value(self, config, cfile=None, plugin_type=None, plugin_name=None, keys=None, variables=None, direct=None):
        """ wrapper """

        try:
            value, _drop = self.get_config_value_and_origin(config, cfile=cfile, plugin_type=plugin_type, plugin_name=plugin_name,
                                                            keys=keys, variables=variables, direct=direct)
        except AnsibleError:
            raise
        except Exception as e:
            raise AnsibleError("Unhandled exception when retrieving %s:\n%s" % (config, to_native(e)), orig_exc=e)
        return value

    def get_config_value_and_origin(self, config, cfile=None, plugin_type=None, plugin_name=None, keys=None, variables=None, direct=None):
        """Get configuration value and its origin with optimized logic"""
        if config == 'CONFIG_FILE':
            return self._config_file if cfile is None else cfile, ''

        defs = self.get_configuration_definitions(plugin_type=plugin_type, name=plugin_name)
        if config not in defs:
            raise AnsibleError(f'Requested entry ({_get_entry(plugin_type, plugin_name, config)}) was not defined in configuration.')

        value = None
        origin = None
        origin_ftype = None
        config_def = defs[config]
        
        # Process sources in order of precedence
        sources = [
            ('direct', lambda: (direct.get(config) if config in direct else next((direct[alias] for alias in config_def.get('aliases', []) if alias in direct), None), 'Direct')),
            ('vars', lambda: self._loop_entries(variables, config_def.get('vars', [])) if variables and config_def.get('vars') else (None, None)),
            ('keyword', lambda: self._loop_entries(keys, config_def.get('keyword', [])) if keys and config_def.get('keyword') else (None, None)),
            ('cli', lambda: self._loop_entries(context.CLIARGS, config_def.get('cli', [])) if 'cli' in config_def else (None, None)),
            ('env', lambda: self._loop_entries(os.environ, config_def.get('env', [])) if config_def.get('env') else (None, None)),
        ]

        for source_type, get_value in sources:
            if value is None:
                value, src = get_value()
                if value is not None:
                    origin = f'{source_type}: {src}' if src else source_type

        # Handle config file if value still not found
        if value is None and cfile is not None:
            if self._parsers.get(cfile) is None:
                self._parse_config_file(cfile)
            
            ftype = get_config_type(cfile)
            if ftype and config_def.get(ftype):
                value, origin, origin_ftype = self._get_value_from_config_file(cfile, config_def[ftype], ftype)

        # Use default if no value found
        if value is None:
            if config_def.get('required', False) and (not plugin_type or config not in INTERNAL_DEFS.get(plugin_type, {})):
                raise AnsibleRequiredOptionError(f"No setting was provided for required configuration {_get_entry(plugin_type, plugin_name, config)}")
            value = self.template_default(config_def.get('default'), variables)
            origin = 'default'

        # Validate type and choices
        try:
            value = ensure_type(value, config_def.get('type'), origin, origin_ftype)
            self._validate_choices(value, config_def, config, plugin_type, plugin_name)
        except ValueError as e:
            if origin.startswith('env:') and value == '':
                value = ensure_type(config_def.get('default'), config_def.get('type'), 'default', origin_ftype)
            else:
                raise AnsibleOptionsError(f'Invalid type for configuration option {_get_entry(plugin_type, plugin_name, config)} (from {origin}): {e}')

        if 'deprecated' in config_def and origin != 'default':
            self.DEPRECATED.append((config, config_def.get('deprecated')))

        return value, origin

    def initialize_plugin_configuration_definitions(self, plugin_type, name, defs):

        if plugin_type not in self._plugins:
            self._plugins[plugin_type] = {}

        self._plugins[plugin_type][name] = defs

    @staticmethod
    def get_deprecated_msg_from_config(dep_docs, include_removal=False, collection_name=None):

        removal = ''
        if include_removal:
            if 'removed_at_date' in dep_docs:
                removal = f"Will be removed in a release after {dep_docs['removed_at_date']}\n\t"
            elif collection_name:
                removal = f"Will be removed in: {collection_name} {dep_docs['removed_in']}\n\t"
            else:
                removal = f"Will be removed in: Ansible {dep_docs['removed_in']}\n\t"

        # TODO: choose to deprecate either singular or plural
        alt = dep_docs.get('alternatives', dep_docs.get('alternative', 'none'))
        return f"Reason: {dep_docs['why']}\n\t{removal}Alternatives: {alt}"
