# Copyright The Caikit Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""This module defines the @schema decorator which can be used to declare data
model objects inline without manually defining the protobufs representation
"""


# Standard
from dataclasses import is_dataclass
from enum import Enum
from functools import update_wrapper
from types import ModuleType
from typing import Any, Callable, List, Type, Union
import importlib
import sys
import types

# Third Party
from google.protobuf import message as _message
from google.protobuf.internal.enum_type_wrapper import EnumTypeWrapper

# First Party
from py_to_proto.dataclass_to_proto import DataclassConverter
import alog
import py_to_proto

# Local
from ..toolkit.errors import error_handler
from . import enums
from .base import DataBase, _DataBaseMetaClass

## Globals #####################################################################

log = alog.use_channel("SCHEMA")
error = error_handler.get(log)

# Registry of auto-generated protos so that they can be rendered to .proto
_AUTO_GEN_PROTO_CLASSES = []


## Public ######################################################################

# Common package prefix
CAIKIT_DATA_MODEL = "caikit_data_model"


def dataobject(*args, **kwargs) -> Callable[[Type], Type[DataBase]]:
    """The @dataobject decorator can be used to define a Data Model object's
    schema inline with the definition of the python class rather than needing to
    bind to a pre-compiled protobufs class. For example:

    @dataobject("foo.bar")
    @dataclass
    class MyDataObject:
        '''My Custom Data Object'''
        foo: str
        bar: int

    NOTE: The wrapped class must NOT inherit directly from DataBase. That
        inheritance will be added by this decorator, but if it is written
        directly, the metaclass that links protobufs to the class will be called
        before this decorator can auto-gen the protobufs class.

    Args:
        package:  str
            The package name to use for the generated protobufs class

    Returns:
        decorator:  Callable[[Type], Type[DataBase]]
            The decorator function that will wrap the given class
    """

    def decorator(cls: Type) -> Type[DataBase]:
        # Make sure that the wrapped class does NOT inherit from DataBase
        error.value_check(
            "<COR95184230E>",
            not issubclass(cls, DataBase),
            "{} should not directly inherit from DataBase when using @schema",
            cls.__name__,
        )

        # Add the package to the kwargs
        kwargs.setdefault("package", package)

        # If there's a schema in the keyword args, use jtd_to_proto
        schema = kwargs.pop("schema", None)
        if schema is not None:
            log.debug("Using JTD To Proto")
            descriptor = py_to_proto.jtd_to_proto(
                name=cls.__name__,
                jtd_def=schema,
                **kwargs,
            )
        # If it's already a dataclass, convert it directly
        elif is_dataclass(cls) or (issubclass(cls, Enum)):
            log.debug("Using dataclass to proto on dataclass")
            descriptor = _dataobject_to_proto(dataclass_=cls, **kwargs)
        # Otherwise, it's not valid
        else:
            raise TypeError(f"Invalid class {cls} is not a dataclass")

        # Create the message class from the dataclass
        proto_class = py_to_proto.descriptor_to_message_class(descriptor)
        _AUTO_GEN_PROTO_CLASSES.append(proto_class)

        # Add enums to the global enums module
        for enum_class in _get_all_enums(proto_class):
            log.debug2("Importing enum [%s]", enum_class.DESCRIPTOR.name)
            enums.import_enum(enum_class)

        # Declare the merged class that binds DataBase to the wrapped class with
        # this generated proto class
        if isinstance(proto_class, type):
            wrapper_class = _make_data_model_class(proto_class, cls)
        else:
            enums.import_enum(proto_class, cls)
            setattr(cls, "_proto_enum", proto_class)
            # for method in ["items", "toDict", "toJSON", "toYAML"]:
            #     setattr(cls, method, getattr(ck_enum, method))
            wrapper_class = cls

        # Attach the proto class to the protobufs module
        parent_mod_name = getattr(cls, "__module__", "").rpartition(".")[0]
        log.debug2("Parent mod name: %s", parent_mod_name)
        if parent_mod_name:
            proto_mod_name = ".".join([parent_mod_name, "protobufs"])
            try:
                proto_mod = importlib.import_module(proto_mod_name)
            except ImportError:
                log.debug("Creating new protobufs module: %s", proto_mod_name)
                proto_mod = ModuleType(proto_mod_name)
                sys.modules[proto_mod_name] = proto_mod
            setattr(proto_mod, cls.__name__, proto_class)

        # Return the merged data class
        return wrapper_class

    # If called without the function invocation, fill in the default argument
    if len(args) and callable(args[0]):
        assert not kwargs, "This shouldn't happen!"
        package = CAIKIT_DATA_MODEL
        return decorator(args[0])

    # Pull the package as an arg or a keyword arg
    if args:
        package = args[0]
    else:
        package = kwargs.get("package", CAIKIT_DATA_MODEL)
    return decorator


def render_dataobject_protos(interfaces_dir: str):
    """Write out protobufs files for all proto classes generated from dataobjects
    to the target interfaces directory

    Args:
        interfaces_dir:  str
            The target directory (must already exist)
    """
    for proto_class in _AUTO_GEN_PROTO_CLASSES:
        proto_class.write_proto_file(interfaces_dir)


## Implementation Details ######################################################


class _EnumBaseSentinel:
    """This base class is used to provide a common base class for enum warpper
    classes so that they can be identified generically
    """


def _dataobject_to_proto(*args, **kwargs):
    return _DataobjectConverter(*args, **kwargs).descriptor


class _DataobjectConverter(DataclassConverter):
    """Augment the dataclass converter to be able to pull descriptors from
    existing data objects
    """

    def get_concrete_type(self, entry: Any) -> Any:
        """Also include data model classes and enums as concrete types"""
        if (isinstance(entry, type) and issubclass(entry, DataBase)) or hasattr(
            entry, "_proto_enum"
        ):
            return entry
        return super().get_concrete_type(entry)

    def get_descriptor(self, entry: Any) -> Any:
        """Unpack data model classes and enums to their descriptors"""
        if isinstance(entry, type) and issubclass(entry, DataBase):
            return entry._proto_class.DESCRIPTOR
        proto_enum = getattr(entry, "_proto_enum", None)
        if proto_enum is not None:
            return proto_enum.DESCRIPTOR
        return super().get_descriptor(entry)


def _get_all_enums(
    proto_class: Union[_message.Message, EnumTypeWrapper],
) -> List[EnumTypeWrapper]:
    """Given a generated proto class, recursively extract all enums"""
    all_enums = []
    if isinstance(proto_class, EnumTypeWrapper):
        all_enums.append(proto_class)
    else:
        for enum_descriptor in proto_class.DESCRIPTOR.enum_types:
            all_enums.append(getattr(proto_class, enum_descriptor.name))
        for nested_proto_descriptor in proto_class.DESCRIPTOR.nested_types:
            all_enums.extend(
                _get_all_enums(getattr(proto_class, nested_proto_descriptor.name))
            )

    return all_enums


def _make_data_model_class(proto_class, wrapped_cls):
    wrapper_cls = _DataBaseMetaClass(
        wrapped_cls.__name__,
        tuple([DataBase, wrapped_cls]),
        {"_proto_class": proto_class, **wrapped_cls.__dict__},
    )
    update_wrapper(wrapper_cls, wrapped_cls, updated=())

    # Recursively make all nested message wrappers
    for nested_message_descriptor in proto_class.DESCRIPTOR.nested_types:
        nested_message_name = nested_message_descriptor.name
        nested_proto_class = getattr(proto_class, nested_message_name)
        setattr(
            wrapper_cls,
            nested_message_name,
            _make_data_model_class(
                nested_proto_class,
                types.new_class(nested_message_name),
            ),
        )
    for nested_enum_descriptor in proto_class.DESCRIPTOR.enum_types:
        setattr(
            wrapper_cls,
            nested_enum_descriptor.name,
            getattr(enums, nested_enum_descriptor.name),
        )

    return wrapper_cls
