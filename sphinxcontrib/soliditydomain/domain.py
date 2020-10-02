import re
from collections import namedtuple
from docutils import nodes
from sphinx import addnodes
from sphinx.directives import ObjectDescription
from sphinx.domains import Domain, ObjType
from sphinx.locale import _
from sphinx.roles import XRefRole
from sphinx.util.docfields import Field, GroupedField, TypedField
from sphinx.util.nodes import make_refnode

from sphinx.util.logging import getLogger

logger = getLogger(__name__)

SolObjFullName = namedtuple("SolObjFullName", ("name", "obj_path", "param_types"))


def fullname2namepath(fullname):
    return ".".join(fullname.obj_path + (fullname.name,))


def fullname2id(fullname):
    return fullname2namepath(fullname) + (
        ""
        if fullname.param_types is None
        else "(" + ",".join(fullname.param_types) + ")"
    )


class SolidityObject(ObjectDescription):
    def add_target_and_index(self, fullname, sig, signode):
        if fullname not in self.state.document.ids:
            signode["ids"].append(fullname2id(fullname))
            self.state.document.note_explicit_target(signode)
            objects = self.env.domaindata["sol"]["objects"]
            if fullname in objects:
                self.state_machine.reporter.warning(
                    "duplicate {type} description of {fullname}, "
                    "other instance in {otherloc}".format(
                        type=self.objtype,
                        fullname=fullname,
                        otherloc=objects[fullname][0],
                    ),
                    line=self.lineno,
                )
            objects[fullname] = (self.env.docname, self.objtype)

        indextext = "{} ({})".format(fullname2namepath(fullname), _(self.objtype))
        if (
            self.objtype == "constructor"
            or self.objtype == "function"
            and fullname.name == "<fallback>"
        ):
            glossary_classifier = (fullname.obj_path or ("?",))[-1][0].upper()
        else:
            glossary_classifier = fullname.name[:1].upper()
        self.indexnode["entries"].append(
            (
                "single",
                indextext,
                fullname2id(fullname),
                False,
                glossary_classifier,
            )
        )

    def before_content(self):
        if self.names:
            obj_path = self.env.ref_context.setdefault("sol:obj_path", [])
            last_name = self.names.pop()
            if hasattr(last_name, "name"):
                obj_path.append(last_name.name)

    def after_content(self):
        obj_path = self.env.ref_context.setdefault("sol:obj_path", [])
        try:
            obj_path.pop()
        except IndexError:
            pass


contract_re = re.compile(
    r"""\s* (\w+)  # name
        (?: \s+ is \s+
            (\w+ (?:\s*,\s* (?:\w+))*)  # parent contracts
        )? \s*""",
    re.VERBOSE,
)


class SolidityTypeLike(SolidityObject):
    def handle_signature(self, sig, signode):
        match = contract_re.fullmatch(sig)
        if match is None:
            logger.warning("could not parse {}".format(sig))
            return None

        name, parents_str = match.groups()
        parents = (
            [] if parents_str is None else [p.strip() for p in parents_str.split(",")]
        )

        signode += nodes.emphasis(text=self.objtype + " ")
        signode += addnodes.desc_name(text=name)

        if len(parents) > 0:
            signode += nodes.Text(" is " + ", ".join(parents))

        return SolObjFullName(
            name=name,
            obj_path=tuple(self.env.ref_context.get("sol:obj_path", [])),
            param_types=None,
        )


param_var_re = re.compile(
    r"""\s* ( [\w\s\[\]\(\)=>\.]+? ) # type
        (?: \s* \b (
            public | private | internal |
            storage | memory | calldata |
            indexed
        ) )? # modifier
        \s*(\b\w+)? # name
        \s*""",
    re.VERBOSE,
)


def normalize_type(type_str):
    type_str = re.sub(r"\s*(\W)", r"\1", type_str)
    type_str = re.sub(r"(\W)\s*", r"\1", type_str)
    type_str = re.sub(r"(\w)\s+(\w)", r"\1 \2", type_str)
    type_str = type_str.replace("mapping(", "mapping (")
    type_str = type_str.replace("=>", " => ")
    return type_str


class SolidityStateVariable(SolidityObject):
    def handle_signature(self, sig, signode):
        match = param_var_re.fullmatch(sig)

        if match is None:
            logger.warning("could not parse {}".format(sig))
            return None

        # normalize type string
        type_str, visibility, name = match.groups()

        if name is None:
            logger.warning("missing name from {}".format(sig))
            return None

        type_str = normalize_type(type_str)

        signode += addnodes.desc_type(text=type_str + " ")

        if visibility is not None:
            signode += nodes.emphasis(text=visibility + " ")

        signode += addnodes.desc_name(text=name)

        return SolObjFullName(
            name=name,
            obj_path=tuple(self.env.ref_context.get("sol:obj_path", [])),
            param_types=None,
        )


function_re = re.compile(
    r"""\s* (\w+)?  # name
        \s* \( ([^)]*) \)  # paramlist
        \s* ((?:\w+ \s* (?:\([^)]*\))? \s* )*)  # modifiers
        \s*""",
    re.VERBOSE,
)


def _parse_params(paramlist_str):
    params = addnodes.desc_parameterlist()

    if len(paramlist_str.strip()) == 0:
        return params, tuple()

    parammatches = [
        param_var_re.fullmatch(param_str) for param_str in paramlist_str.split(",")
    ]

    if not all(parammatches):
        logger.warning(
            "could not parse all params in parameter list {}".format(paramlist_str)
        )
        return None

    abi_types = []
    for parammatch in parammatches:
        atype, memloc, name = parammatch.groups()
        atype = normalize_type(atype)
        abi_types.append(atype + ("" if memloc != "storage" else " storage"))
        params += addnodes.desc_parameter(
            text=" ".join(filter(lambda x: x, (atype, memloc, name)))
        )

    return params, tuple(abi_types)


modifier_re = re.compile(r"(\w+)(?:\s*\(([^)]*)\))?")


class SolidityFunctionLike(SolidityObject):
    doc_field_types = [
        TypedField(
            "parameter",
            label=_("Parameters"),
            names=("param", "parameter", "arg", "argument"),
            typenames=("type",),
        ),
        TypedField(
            "returnvalue",
            label=_("Returns"),
            names=("return", "returns"),
            typenames=("rtype",),
        ),
    ]

    def handle_signature(self, sig, signode):
        signode.is_multiline = True
        primary_line = addnodes.desc_signature_line(add_permalink=True)
        match = function_re.fullmatch(sig)
        if match is None:
            logger.warning("could not parse {}".format(sig))
            return None

        name, paramlist_str, modifiers_str = match.groups()

        if name is None:
            if self.objtype == "constructor":
                name = "constructor"
                primary_line += addnodes.desc_name(text=self.objtype)
            elif self.objtype == "function":
                name = "<fallback>"
                primary_line += addnodes.desc_name(text=_("<fallback>"))
                primary_line += nodes.emphasis(text=" " + self.objtype)
                if len(paramlist_str.strip()) != 0:
                    logger.warning(
                        "fallback function must have no parameters, but instead got parameter list {}".format(
                            paramlist_str
                        )
                    )
                    return None
            else:
                logger.warning("{} must have name".format(self.objtype))
                return None
        else:
            primary_line += nodes.emphasis(text=self.objtype + " ")
            primary_line += addnodes.desc_name(text=name)

        params_parameter_list, param_types = _parse_params(paramlist_str)
        primary_line += params_parameter_list
        signode += primary_line

        if self.objtype == "modifier" and len(modifiers_str.strip()) != 0:
            logger.warning("modifier {} can't have modifiers".format(name))
            return None

        for match in modifier_re.finditer(modifiers_str):
            modname, modparams_str = match.groups()
            newline = addnodes.desc_signature_line()
            newline += nodes.Text(" ")  # HACK: special whitespace :/
            if modname in (
                "public",
                "private",
                "external",
                "internal",
                "pure",
                "view",
                "payable",
                "anonymous",
            ):
                newline += nodes.emphasis(text=modname)
                if modparams_str is not None:
                    logger.warning("keyword {} can't have arguments".format(modname))
                    return None
            elif modname == "returns":
                newline += nodes.emphasis(text=modname + " ")
                if modparams_str is not None:
                    newline += _parse_params(modparams_str)[0]
            else:
                newline += nodes.Text(modname)
                if modparams_str is not None:
                    modparamlist = addnodes.desc_parameterlist()
                    for modparam in modparams_str.split(","):
                        modparam = modparam.strip()
                        if modparam:
                            modparamlist += addnodes.desc_parameter(text=modparam)
                    newline += modparamlist

            signode += newline

        if self.objtype not in ("function", "event"):
            param_types = None

        return SolObjFullName(
            name=name,
            obj_path=tuple(self.env.ref_context.get("sol:obj_path", [])),
            param_types=param_types,
        )


class SolidityStruct(SolidityTypeLike):
    doc_field_types = [
        TypedField(
            "member", label=_("Members"), names=("member",), typenames=("type",)
        ),
    ]


class SolidityEnum(SolidityTypeLike):
    doc_field_types = [
        GroupedField("member", label=_("Members"), names=("member",)),
    ]


class SolidityXRefRole(XRefRole):
    def process_link(self, env, refnode, has_explicit_title, title, target):
        # type: (BuildEnvironment, nodes.reference, bool, unicode, unicode) -> Tuple[unicode, unicode]  # NOQA
        """Called after parsing title and target text, and creating the
        reference node (given in *refnode*).  This method can alter the
        reference node and must return a new (or the same) ``(title, target)``
        tuple.
        """
        return title, target


class SolidityDomain(Domain):
    """Solidity language domain."""

    name = "sol"
    label = "Solidity"

    directives = {
        "contract": SolidityTypeLike,
        "library": SolidityTypeLike,
        "interface": SolidityTypeLike,
        "statevar": SolidityStateVariable,
        "constructor": SolidityFunctionLike,
        "function": SolidityFunctionLike,
        "modifier": SolidityFunctionLike,
        "event": SolidityFunctionLike,
        "struct": SolidityStruct,
        "enum": SolidityEnum,
    }

    roles = {
        "contract": SolidityXRefRole(),
        "lib": SolidityXRefRole(),
        "interface": SolidityXRefRole(),
        "svar": SolidityXRefRole(),
        "cons": SolidityXRefRole(),
        "func": SolidityXRefRole(),
        "mod": SolidityXRefRole(),
        "event": SolidityXRefRole(),
        "struct": SolidityXRefRole(),
        "enum": SolidityXRefRole(),
    }

    initial_data = {
        "objects": {},
    }

    def clear_doc(self, docname):
        # type: (unicode) -> None
        for fullname, (fn, _l) in list(self.data["objects"].items()):
            if fn == docname:
                del self.data["objects"][fullname]

    def merge_domaindata(self, docnames, otherdata):
        # type: (List[unicode], Dict) -> None
        # XXX check duplicates
        for fullname, (fn, objtype) in otherdata["objects"].items():
            if fn in docnames:
                self.data["objects"][fullname] = (fn, objtype)

    def resolve_xref(self, env, fromdocname, builder, typ, target, node, contnode):
        # type: (BuildEnvironment, unicode, Builder, unicode, unicode, nodes.Node, nodes.Node) -> nodes.Node  # NOQA
        """Resolve the pending_xref *node* with the given *typ* and *target*.

        This method should return a new node, to replace the xref node,
        containing the *contnode* which is the markup content of the
        cross-reference.

        If no resolution can be found, None can be returned; the xref node will
        then given to the :event:`missing-reference` event, and if that yields no
        resolution, replaced by *contnode*.

        The method can also raise :exc:`sphinx.environment.NoUri` to suppress
        the :event:`missing-reference` event being emitted.
        """
        for fullname, (docname, objtype) in self.data["objects"].items():
            if fullname.name == target:
                return make_refnode(
                    builder,
                    fromdocname,
                    docname,
                    fullname2id(fullname),
                    contnode,
                    fullname.name,
                )
        return None
