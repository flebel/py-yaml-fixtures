import os
import re
import yaml

from collections import defaultdict
from faker import Faker
from jinja2 import Environment, FileSystemLoader
from typing import *

from .factory_interface import FactoryInterface
from .types import AttrDict, Identifier


identifier_re = re.compile('(?P<class_name>\w+)\((?P<identifiers>[\w,\s]+)\)')


class FixturesLoader:
    """
    The factory "driver" class. Does most of the hard work of loading fixtures,
    leaving the responsibility of model instantiation up to the factory class
    passed in.
    """

    # The Jinja Environment
    env = None

    # A set of the class names whose fixture files have already been loaded
    loaded_class_names = set()

    # A dictionary of identifier keys to their class names
    class_name_lookup = {}

    # A dictionary of identifier keys to their raw data from the yaml files
    model_fixtures = {}

    def __init__(self, factory: FactoryInterface,
                 fixtures_dir: str,
                 env: Optional[Environment] = None,):
        """
        :param factory: An instance of the concrete factory to use for creating
                        models
        :param fixtures_dir: Path to folder to load template fixtures from
        :param env: An optional jinja environment (the default one will include
                    faker as a template global, but if you want to customize its
                    tags/filters/etc, then you need to create an env yourself - the
                    correct loader will be set automatically for you)
        """
        factory.loader = self
        self.factory = factory
        self.env = env
        self.fixtures_dir = fixtures_dir

    def get_models(self, identifiers: Union[str, List[str]]):
        """
        Returns a dictionary of all models in the fixtures.

        :param identifiers: A single identifier string, or a list of identifier
                            strings
        :return: An object where the attributes are the identifier keys, and the
                 values are model instances
        """
        return AttrDict(self.create_all(identifiers))

    def create_all(self,
                   identifiers: Optional[Union[str, List[str]]] = None,
                   ) -> Dict[str, object]:
        """
        Create all models found in fixtures (or, if passed the ``identifiers``
        parameter, it will only create those listed)

        :param identifiers: An identifier string, or a list of identifier strings
        :return: A dictionary of identifier key to model instances
        """
        if identifiers:
            identifiers = self._flatten_identifiers(identifiers)
        else:
            self._load_data()
            identifiers = [Identifier(class_name, identifier)
                           for identifier, class_name in
                           self.class_name_lookup.items()]

        models = {identifier.key: self._create(identifier)
                  for identifier in identifiers}
        self.factory.commit()
        return models

    def create(self, class_name_or_identifier_string: str,
               identifier_key: Optional[str] = None) -> object:
        """
        Create a single model.

        :param class_name_or_identifier_string: Either an identifier string,
                                                or a class name
        :param identifier_key: If the first argument was a class name, then
                               this is required. Otherwise, it's ignored.
        :return: A model instance
        """
        if identifier_key:
            class_name = class_name_or_identifier_string
            identifier = Identifier(class_name, identifier_key)
        else:
            identifier_string = class_name_or_identifier_string
            identifier = _convert_str(identifier_string)[0]

        data = self.model_fixtures[identifier.key]
        model = self.factory.create_or_update(identifier, data)
        self.factory.commit()
        return model

    def _create(self, identifier: Identifier) -> object:
        if not identifier.class_name:
            raise Exception('Identifier must have a class name!')
        self._maybe_load_data([identifier])
        data = self.factory.maybe_convert_values(
            identifier, self.model_fixtures[identifier.key])
        return self.factory.create_or_update(identifier, data)

    def convert_identifiers(self, identifiers: Union[str, List[str]],
                            ) -> Union[object, List[object]]:
        """
        Converts identifier strings into the appropriate model instances

        :param identifiers: An identifier string, or a list of identifier strings
        :return: The converted model instance(s)
        """
        if isinstance(identifiers, list):
            return [self._create(identifier)
                    for identifier in self._flatten_identifiers(identifiers)]
        return self.convert_identifier(identifiers)

    def convert_identifier(self, identifier: str) -> Union[object, List[object]]:
        result = [self._create(identifier)
                  for identifier in self._flatten_identifiers(identifier)]
        return result[0] if len(result) == 1 else result

    def _maybe_load_data(self, identifiers: List[Identifier]):
        class_names = {class_name for class_name, _ in identifiers}
        class_names = class_names.difference(self.loaded_class_names)
        if not class_names:
            return
        self._load_data(class_names)

    def _load_data(self, class_names: Optional[Set[str]] = None):
        for filename in os.listdir(self.fixtures_dir):
            path = os.path.join(self.fixtures_dir, filename)
            if os.path.isfile(path):
                class_name = filename[:filename.rfind('.')]
                if (not class_names
                        or None in class_names
                        or class_name in class_names):
                    self._load_from_yaml(filename)
                    self.loaded_class_names.add(class_name)

    def _load_from_yaml(self, filename: str):
        self._ensure_env()
        rendered_yaml = self.env.get_template(filename).render()
        fixture_data = yaml.load(rendered_yaml)

        class_name = filename[:filename.rfind('.')]
        for identifier_id, data in fixture_data.items():
            # FIXME check for dups
            self.class_name_lookup[identifier_id] = class_name
            self.model_fixtures[identifier_id] = data

    def _ensure_env(self):
        if not self.env:
            self.env = Environment()
        if not self.env.loader:
            self.env.loader = FileSystemLoader(self.fixtures_dir)
        if 'faker' not in self.env.globals:
            faker = Faker()
            faker.seed(1234)
            self.env.globals['faker'] = faker

    def _flatten_identifiers(self,
                             identifiers: Union[str, List[str]],
                             ) -> List[Identifier]:
        if isinstance(identifiers, str):
            identifiers = _convert_str(identifiers)
        if isinstance(identifiers, (list, tuple)):
            identifiers = _group_by_class_name(identifiers)

        rv = {}
        for class_name, values in identifiers.items():
            for key in _flatten_csv_list(values):
                if not key:
                    continue
                if not class_name:
                    class_name = self.class_name_lookup[key]
                identifier = Identifier(class_name, key)
                rv[identifier.key] = identifier  # ensure unique identifiers
        return list(rv.values())


def _group_by_class_name(identifiers: List[str]) -> DefaultDict[str, List[str]]:
    rv = defaultdict(list)
    for v in identifiers:
        if isinstance(v, Identifier):
            rv[v.class_name].append(v.key)
        elif isinstance(v, str):
            for identifier in _convert_str(v):
                rv[identifier.class_name].append(identifier.key)
        else:
            raise Exception(
                'Unexpected type {t} (for {v!r})'.format(t=type(v), v=v))
    return rv


def _flatten_csv_list(identifier_keys: List[str]) -> List[str]:
    return [key.strip()
            for keys in identifier_keys
            for key in keys.strip(',').split(',')]


def _convert_str(value: str) -> List[Identifier]:
    value = ''.join(value.splitlines())
    rv = []
    prev = None
    while True:
        match = identifier_re.search(value, prev.end() if prev else 0)
        if not match and not rv:
            return [Identifier(None, value)]
        elif not match:
            return rv

        rv.append(Identifier(match.group('class_name'),
                             match.group('identifiers')))
        prev = match
