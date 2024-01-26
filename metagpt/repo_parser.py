#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2023/11/17 17:58
@Author  : alexanderwu
@File    : repo_parser.py
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from pydantic import BaseModel, Field, field_validator

from metagpt.const import AGGREGATION, COMPOSITION, GENERALIZATION
from metagpt.logs import logger
from metagpt.utils.common import any_to_str, aread
from metagpt.utils.exceptions import handle_exception


class RepoFileInfo(BaseModel):
    file: str
    classes: List = Field(default_factory=list)
    functions: List = Field(default_factory=list)
    globals: List = Field(default_factory=list)
    page_info: List = Field(default_factory=list)


class CodeBlockInfo(BaseModel):
    lineno: int
    end_lineno: int
    type_name: str
    tokens: List = Field(default_factory=list)
    properties: Dict = Field(default_factory=dict)


class DotClassAttribute(BaseModel):
    name: str = ""
    type_: str = ""
    default_: str = ""
    description: str
    compositions: List[str] = Field(default_factory=list)

    @classmethod
    def parse(cls, v: str) -> "DotClassAttribute":
        val = ""
        meet_colon = False
        meet_equals = False
        for c in v:
            if c == ":":
                meet_colon = True
            elif c == "=":
                meet_equals = True
                if not meet_colon:
                    val += ":"
                    meet_colon = True
            val += c
        if not meet_colon:
            val += ":"
        if not meet_equals:
            val += "="

        cix = val.find(":")
        eix = val.rfind("=")
        name = val[0:cix].strip()
        type_ = val[cix + 1 : eix]
        default_ = val[eix + 1 :].strip()

        type_ = cls.remove_white_spaces(type_)  # remove white space
        if type_ == "NoneType":
            type_ = ""
        if "Literal[" in type_:
            pre_l, literal, post_l = cls._split_literal(type_)
            composition_val = pre_l + "Literal" + post_l  # replace Literal[...] with Literal
            type_ = pre_l + literal + post_l
        else:
            type_ = re.sub(r"['\"]", "", type_)  # remove '"
            composition_val = type_

        if default_ == "None":
            default_ = ""
        compositions = cls.parse_compositions(composition_val)
        return cls(name=name, type_=type_, default_=default_, description=v, compositions=compositions)

    @staticmethod
    def remove_white_spaces(v: str):
        return re.sub(r"(?<!['\"])\s|(?<=['\"])\s", "", v)

    @staticmethod
    def parse_compositions(types_part) -> List[str]:
        if not types_part:
            return []
        modified_string = re.sub(r"[\[\],]", "|", types_part)
        types = modified_string.split("|")
        filters = {
            "str",
            "frozenset",
            "set",
            "int",
            "float",
            "complex",
            "bool",
            "dict",
            "list",
            "Union",
            "Dict",
            "Set",
            "Tuple",
            "NoneType",
            "None",
            "Any",
            "Optional",
            "Iterator",
            "Literal",
            "List",
        }
        result = set()
        for t in types:
            t = re.sub(r"['\"]", "", t.strip())
            if t and t not in filters:
                result.add(t)
        return list(result)

    @staticmethod
    def _split_literal(v):
        tag = "Literal["
        bix = v.find(tag)
        eix = len(v) - 1
        counter = 1
        for i in range(bix + len(tag), len(v) - 1):
            c = v[i]
            if c == "[":
                counter += 1
                continue
            if c == "]":
                counter -= 1
                if counter > 0:
                    continue
                eix = i
                break
        pre_l = v[0:bix]
        post_l = v[eix + 1 :]
        pre_l = re.sub(r"['\"]", "", pre_l)  # remove '"
        pos_l = re.sub(r"['\"]", "", post_l)  # remove '"

        return pre_l, v[bix : eix + 1], pos_l

    @field_validator("compositions", mode="after")
    @classmethod
    def sort(cls, lst: List) -> List:
        lst.sort()
        return lst


class DotClassInfo(BaseModel):
    name: str
    package: Optional[str] = None
    attributes: Dict[str, DotClassAttribute] = Field(default_factory=dict)
    methods: Dict[str, DotClassMethod] = Field(default_factory=dict)
    compositions: List[str] = Field(default_factory=list)
    aggregations: List[str] = Field(default_factory=list)

    @field_validator("compositions", "aggregations", mode="after")
    @classmethod
    def sort(cls, lst: List) -> List:
        lst.sort()
        return lst


class DotClassRelationship(BaseModel):
    src: str = ""
    dest: str = ""
    relationship: str = ""
    label: Optional[str] = None


class DotReturn(BaseModel):
    type_: str = ""
    description: str
    compositions: List[str] = Field(default_factory=list)

    @classmethod
    def parse(cls, v: str) -> "DotReturn" | None:
        if not v:
            return DotReturn(description=v)
        type_ = DotClassAttribute.remove_white_spaces(v)
        compositions = DotClassAttribute.parse_compositions(type_)
        return cls(type_=type_, description=v, compositions=compositions)

    @field_validator("compositions", mode="after")
    @classmethod
    def sort(cls, lst: List) -> List:
        lst.sort()
        return lst


class DotClassMethod(BaseModel):
    name: str
    args: List[DotClassAttribute] = Field(default_factory=list)
    return_args: Optional[DotReturn] = None
    description: str
    aggregations: List[str] = Field(default_factory=list)

    @classmethod
    def parse(cls, v: str) -> "DotClassMethod":
        bix = v.find("(")
        eix = v.rfind(")")
        rix = v.rfind(":")
        if rix < 0 or rix < eix:
            rix = eix
        name_part = v[0:bix].strip()
        args_part = v[bix + 1 : eix].strip()
        return_args_part = v[rix + 1 :].strip()

        name = cls._parse_name(name_part)
        args = cls._parse_args(args_part)
        return_args = DotReturn.parse(return_args_part)
        aggregations = set()
        for i in args:
            aggregations.update(set(i.compositions))
        aggregations.update(set(return_args.compositions))

        return cls(name=name, args=args, description=v, return_args=return_args, aggregations=list(aggregations))

    @staticmethod
    def _parse_name(v: str) -> str:
        tags = [">", "</"]
        if tags[0] in v:
            bix = v.find(tags[0]) + len(tags[0])
            eix = v.rfind(tags[1])
            return v[bix:eix].strip()
        return v.strip()

    @staticmethod
    def _parse_args(v: str) -> List[DotClassAttribute]:
        if not v:
            return []
        parts = []
        bix = 0
        counter = 0
        for i in range(0, len(v)):
            c = v[i]
            if c == "[":
                counter += 1
                continue
            elif c == "]":
                counter -= 1
                continue
            elif c == "," and counter == 0:
                parts.append(v[bix:i].strip())
                bix = i + 1
        parts.append(v[bix:].strip())

        attrs = []
        for p in parts:
            if p:
                attr = DotClassAttribute.parse(p)
                attrs.append(attr)
        return attrs


class RepoParser(BaseModel):
    base_directory: Path = Field(default=None)

    @classmethod
    @handle_exception(exception_type=Exception, default_return=[])
    def _parse_file(cls, file_path: Path) -> list:
        """Parse a Python file in the repository."""
        return ast.parse(file_path.read_text()).body

    def extract_class_and_function_info(self, tree, file_path) -> RepoFileInfo:
        """Extract class, function, and global variable information from the AST."""
        file_info = RepoFileInfo(file=str(file_path.relative_to(self.base_directory)))
        for node in tree:
            info = RepoParser.node_to_str(node)
            if info:
                file_info.page_info.append(info)
            if isinstance(node, ast.ClassDef):
                class_methods = [m.name for m in node.body if is_func(m)]
                file_info.classes.append({"name": node.name, "methods": class_methods})
            elif is_func(node):
                file_info.functions.append(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    if isinstance(target, ast.Name):
                        file_info.globals.append(target.id)
        return file_info

    def generate_symbols(self) -> List[RepoFileInfo]:
        files_classes = []
        directory = self.base_directory

        matching_files = []
        extensions = ["*.py", "*.js"]
        for ext in extensions:
            matching_files += directory.rglob(ext)
        for path in matching_files:
            tree = self._parse_file(path)
            file_info = self.extract_class_and_function_info(tree, path)
            files_classes.append(file_info)

        return files_classes

    def generate_json_structure(self, output_path):
        """Generate a JSON file documenting the repository structure."""
        files_classes = [i.model_dump() for i in self.generate_symbols()]
        output_path.write_text(json.dumps(files_classes, indent=4))

    def generate_dataframe_structure(self, output_path):
        """Generate a DataFrame documenting the repository structure and save as CSV."""
        files_classes = [i.model_dump() for i in self.generate_symbols()]
        df = pd.DataFrame(files_classes)
        df.to_csv(output_path, index=False)

    def generate_structure(self, output_path=None, mode="json") -> Path:
        """Generate the structure of the repository as a specified format."""
        output_file = self.base_directory / f"{self.base_directory.name}-structure.{mode}"
        output_path = Path(output_path) if output_path else output_file

        if mode == "json":
            self.generate_json_structure(output_path)
        elif mode == "csv":
            self.generate_dataframe_structure(output_path)
        return output_path

    @staticmethod
    def node_to_str(node) -> CodeBlockInfo | None:
        if isinstance(node, ast.Try):
            return None
        if any_to_str(node) == any_to_str(ast.Expr):
            return CodeBlockInfo(
                lineno=node.lineno,
                end_lineno=node.end_lineno,
                type_name=any_to_str(node),
                tokens=RepoParser._parse_expr(node),
            )
        mappings = {
            any_to_str(ast.Import): lambda x: [RepoParser._parse_name(n) for n in x.names],
            any_to_str(ast.Assign): RepoParser._parse_assign,
            any_to_str(ast.ClassDef): lambda x: x.name,
            any_to_str(ast.FunctionDef): lambda x: x.name,
            any_to_str(ast.ImportFrom): lambda x: {
                "module": x.module,
                "names": [RepoParser._parse_name(n) for n in x.names],
            },
            any_to_str(ast.If): RepoParser._parse_if,
            any_to_str(ast.AsyncFunctionDef): lambda x: x.name,
            any_to_str(ast.AnnAssign): lambda x: RepoParser._parse_variable(x.target),
        }
        func = mappings.get(any_to_str(node))
        if func:
            code_block = CodeBlockInfo(lineno=node.lineno, end_lineno=node.end_lineno, type_name=any_to_str(node))
            val = func(node)
            if isinstance(val, dict):
                code_block.properties = val
            elif isinstance(val, list):
                code_block.tokens = val
            elif isinstance(val, str):
                code_block.tokens = [val]
            else:
                raise NotImplementedError(f"Not implement:{val}")
            return code_block
        logger.warning(f"Unsupported code block:{node.lineno}, {node.end_lineno}, {any_to_str(node)}")
        return None

    @staticmethod
    def _parse_expr(node) -> List:
        funcs = {
            any_to_str(ast.Constant): lambda x: [any_to_str(x.value), RepoParser._parse_variable(x.value)],
            any_to_str(ast.Call): lambda x: [any_to_str(x.value), RepoParser._parse_variable(x.value.func)],
        }
        func = funcs.get(any_to_str(node.value))
        if func:
            return func(node)
        raise NotImplementedError(f"Not implement: {node.value}")

    @staticmethod
    def _parse_name(n):
        if n.asname:
            return f"{n.name} as {n.asname}"
        return n.name

    @staticmethod
    def _parse_if(n):
        tokens = []
        try:
            if isinstance(n.test, ast.BoolOp):
                tokens = []
                for v in n.test.values:
                    tokens.extend(RepoParser._parse_if_compare(v))
                return tokens
            if isinstance(n.test, ast.Compare):
                v = RepoParser._parse_variable(n.test.left)
                if v:
                    tokens.append(v)
            for item in n.test.comparators:
                v = RepoParser._parse_variable(item)
                if v:
                    tokens.append(v)
            return tokens
        except Exception as e:
            logger.warning(f"Unsupported if: {n}, err:{e}")
        return tokens

    @staticmethod
    def _parse_if_compare(n):
        if hasattr(n, "left"):
            return RepoParser._parse_variable(n.left)
        else:
            return []

    @staticmethod
    def _parse_variable(node):
        try:
            funcs = {
                any_to_str(ast.Constant): lambda x: x.value,
                any_to_str(ast.Name): lambda x: x.id,
                any_to_str(ast.Attribute): lambda x: f"{x.value.id}.{x.attr}"
                if hasattr(x.value, "id")
                else f"{x.attr}",
                any_to_str(ast.Call): lambda x: RepoParser._parse_variable(x.func),
                any_to_str(ast.Tuple): lambda x: "",
            }
            func = funcs.get(any_to_str(node))
            if not func:
                raise NotImplementedError(f"Not implement:{node}")
            return func(node)
        except Exception as e:
            logger.warning(f"Unsupported variable:{node}, err:{e}")

    @staticmethod
    def _parse_assign(node):
        return [RepoParser._parse_variable(t) for t in node.targets]

    async def rebuild_class_views(self, path: str | Path = None):
        if not path:
            path = self.base_directory
        path = Path(path)
        if not path.exists():
            return
        command = f"pyreverse {str(path)} -o dot"
        result = subprocess.run(command, shell=True, check=True, cwd=str(path))
        if result.returncode != 0:
            raise ValueError(f"{result}")
        class_view_pathname = path / "classes.dot"
        class_views = await self._parse_classes(class_view_pathname)
        relationship_views = await self._parse_class_relationships(class_view_pathname)
        packages_pathname = path / "packages.dot"
        class_views, relationship_views, package_root = RepoParser._repair_namespaces(
            class_views=class_views, relationship_views=relationship_views, path=path
        )
        class_view_pathname.unlink(missing_ok=True)
        packages_pathname.unlink(missing_ok=True)
        return class_views, relationship_views, package_root

    async def _parse_classes(self, class_view_pathname):
        class_views = []
        if not class_view_pathname.exists():
            return class_views
        data = await aread(filename=class_view_pathname, encoding="utf-8")
        lines = data.split("\n")
        for line in lines:
            package_name, info = RepoParser._split_class_line(line)
            if not package_name:
                continue
            class_name, members, functions = re.split(r"(?<!\\)\|", info)
            class_info = DotClassInfo(name=class_name)
            class_info.package = package_name
            for m in members.split("\n"):
                if not m:
                    continue
                attr = DotClassAttribute.parse(m)
                class_info.attributes[attr.name] = attr
                for i in attr.compositions:
                    if i not in class_info.compositions:
                        class_info.compositions.append(i)
            for f in functions.split("\n"):
                if not f:
                    continue
                method = DotClassMethod.parse(f)
                class_info.methods[method.name] = method
                for i in method.aggregations:
                    if i not in class_info.compositions and i not in class_info.aggregations:
                        class_info.aggregations.append(i)
            class_views.append(class_info)
        return class_views

    async def _parse_class_relationships(self, class_view_pathname) -> List[DotClassRelationship]:
        relationship_views = []
        if not class_view_pathname.exists():
            return relationship_views
        data = await aread(filename=class_view_pathname, encoding="utf-8")
        lines = data.split("\n")
        for line in lines:
            relationship = RepoParser._split_relationship_line(line)
            if not relationship:
                continue
            relationship_views.append(relationship)
        return relationship_views

    @staticmethod
    def _split_class_line(line):
        part_splitor = '" ['
        if part_splitor not in line:
            return None, None
        ix = line.find(part_splitor)
        class_name = line[0:ix].replace('"', "")
        left = line[ix:]
        begin_flag = "label=<{"
        end_flag = "}>"
        if begin_flag not in left or end_flag not in left:
            return None, None
        bix = left.find(begin_flag)
        eix = left.rfind(end_flag)
        info = left[bix + len(begin_flag) : eix]
        info = re.sub(r"<br[^>]*>", "\n", info)
        return class_name, info

    @staticmethod
    def _split_relationship_line(line):
        splitters = [" -> ", " [", "];"]
        idxs = []
        for tag in splitters:
            if tag not in line:
                return None
            idxs.append(line.find(tag))
        ret = DotClassRelationship()
        ret.src = line[0 : idxs[0]].strip('"')
        ret.dest = line[idxs[0] + len(splitters[0]) : idxs[1]].strip('"')
        properties = line[idxs[1] + len(splitters[1]) : idxs[2]].strip(" ")
        mappings = {
            'arrowhead="empty"': GENERALIZATION,
            'arrowhead="diamond"': COMPOSITION,
            'arrowhead="odiamond"': AGGREGATION,
        }
        for k, v in mappings.items():
            if k in properties:
                ret.relationship = v
                if v != GENERALIZATION:
                    ret.label = RepoParser._get_label(properties)
                break
        return ret

    @staticmethod
    def _get_label(line):
        tag = 'label="'
        if tag not in line:
            return ""
        ix = line.find(tag)
        eix = line.find('"', ix + len(tag))
        return line[ix + len(tag) : eix]

    @staticmethod
    def _create_path_mapping(path: str | Path) -> Dict[str, str]:
        mappings = {
            str(path).replace("/", "."): str(path),
        }
        files = []
        try:
            directory_path = Path(path)
            if not directory_path.exists():
                return mappings
            for file_path in directory_path.iterdir():
                if file_path.is_file():
                    files.append(str(file_path))
                else:
                    subfolder_files = RepoParser._create_path_mapping(path=file_path)
                    mappings.update(subfolder_files)
        except Exception as e:
            logger.error(f"Error: {e}")
        for f in files:
            mappings[str(Path(f).with_suffix("")).replace("/", ".")] = str(f)

        return mappings

    @staticmethod
    def _repair_namespaces(
        class_views: List[DotClassInfo], relationship_views: List[DotClassRelationship], path: str | Path
    ) -> (List[DotClassInfo], List[DotClassRelationship], str):
        if not class_views:
            return [], [], ""
        c = class_views[0]
        full_key = str(path).lstrip("/").replace("/", ".")
        root_namespace = RepoParser._find_root(full_key, c.package)
        root_path = root_namespace.replace(".", "/")

        mappings = RepoParser._create_path_mapping(path=path)
        new_mappings = {}
        ix_root_namespace = len(root_namespace)
        ix_root_path = len(root_path)
        for k, v in mappings.items():
            nk = k[ix_root_namespace:]
            nv = v[ix_root_path:]
            new_mappings[nk] = nv

        for c in class_views:
            c.package = RepoParser._repair_ns(c.package, new_mappings)
        for i in range(len(relationship_views)):
            v = relationship_views[i]
            v.src = RepoParser._repair_ns(v.src, new_mappings)
            v.dest = RepoParser._repair_ns(v.dest, new_mappings)
            relationship_views[i] = v
        return class_views, relationship_views, root_path

    @staticmethod
    def _repair_ns(package, mappings):
        file_ns = package
        while file_ns != "":
            if file_ns not in mappings:
                ix = file_ns.rfind(".")
                file_ns = file_ns[0:ix]
                continue
            break
        internal_ns = package[ix + 1 :]
        ns = mappings[file_ns] + ":" + internal_ns.replace(".", ":")
        return ns

    @staticmethod
    def _find_root(full_key, package) -> str:
        left = full_key
        while left != "":
            if left in package:
                break
            if "." not in left:
                break
            ix = left.find(".")
            left = left[ix + 1 :]
        ix = full_key.rfind(left)
        return "." + full_key[0:ix]


def is_func(node):
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
