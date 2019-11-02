"""The Gaphor code generator.

This file provides the code generator which transforms gaphor/UML/uml2.gaphor
into gaphor/UML/uml2.py. Also a distutils tool, build_uml, is provided.

Create a UML 2.0 datamodel from the Gaphor model file.

To do this we do the following:
1. read the model file with the gaphor parser
2. Create a object hierarchy by ordering elements based on generalizations

Recreate the model using some very dynamic class, so we can set all
attributes and traverse them to generate the data model.
"""

from gaphor.storage.parser import parse, base, element
from utils.model.writer import msg, Writer
from utils.model import override

header = """# This file is generated by build_uml.py. DO NOT EDIT!

from __future__ import annotations

from typing import List, Callable
from gaphor.UML.properties import (
    association,
    attribute,
    enumeration,
    derived,
    derivedunion,
    relation_one,
    relation_many,
    redefine,
)
from gaphor.UML.collection import collection

"""

# Make getitem behave more politely
base.__real_getitem__ = base.__getitem__


def base__getitem__(self, key):
    try:
        return self.__real_getitem__(key)
    except KeyError:
        return None


base.__getitem__ = base__getitem__


def parse_association_name(name):
    # First remove spaces
    name = name.replace(" ", "")
    derived = False
    # Check if this is a derived union
    while name and not name[0].isalpha():
        if name[0] == "/":
            derived = True
        name = name[1:]
    return derived, name


def parse_association_tags(appliedStereotypes):
    subsets = []
    redefines = None

    for stereotype in appliedStereotypes or []:
        for slot in stereotype.slot or []:

            if slot.definingFeature.name == "subsets":
                value = slot.value
                # remove all whitespaces and stuff
                value = value.replace(" ", "").replace("\n", "").replace("\r", "")
                subsets = value.split(",")

            if slot.definingFeature.name == "redefines":
                value = slot.value
                # remove all whitespaces and stuff
                redefines = value.replace(" ", "").replace("\n", "").replace("\r", "")

    return subsets, redefines


def get_association_ends(a, properties, classes):
    ends = []
    for end in a.memberEnd:
        end = properties[end]
        end.type = classes[end["type"]]
        if end.get("class_"):
            end.class_ = classes[end["class_"]]
        else:
            end.class_ = None
        end.is_simple_attribute = False
        if end.type is not None and end.type.stereotypeName == "SimpleAttribute":
            end.is_simple_attribute = True
            a.asAttribute = end
        ends.append(end)
    return tuple(ends)


def parse_association_end(head, tail):
    """
    The head association end is enriched with the following attributes:

        derived - association is a derived union or not
        name - name of the association end (name of head is found on tail)
        class_name - name of the class this association belongs to
        opposite_class_name - name of the class at the other end of the assoc.
        lower - lower multiplicity
        upper - upper multiplicity
        composite - if the association has a composite relation to the other end
        subsets - derived unions that use the association
        redefines - redefines existing associations
    """
    head.navigable = head.get("class_")
    if not head.navigable:
        # from this side, the association is not navigable
        return

    name = head.name
    if name is None:
        raise ValueError(
            "ERROR! no name, but navigable: %s (%s.%s)"
            % (head.id, head.class_name, head.name)
        )

    upper = head.upperValue or "*"
    lower = head.lowerValue or upper
    if lower == "*":
        lower = 0
    subsets, redefines = parse_association_tags(head.appliedStereotype)

    # Add the values found. These are used later to generate derived unions.
    head.class_name = head.class_["name"]
    head.opposite_class_name = head.type["name"]
    head.lower = lower
    head.upper = upper
    head.subsets = subsets
    head.composite = head.get("aggregation") == "composite"
    head.derived = int(head.isDerived or 0)
    head.redefines = redefines
    # redefines.upper = upper


def filter_out_metaclasses(classes, extensions, all_elements):
    """
    Remove metaclasses from classes dict
    should check for Extension.memberEnd[Property].type
    """
    for e in extensions.values():
        ends = []
        for end in e.memberEnd:
            end = all_elements[end]
            if not end["type"]:
                continue
            end.type = all_elements[end["type"]]
            ends.append(end)
        e.memberEnd = ends
        if ends:
            del classes[e.memberEnd[0].type.id]
    return classes


def filter_out_simple_attributes(classes):
    return {k: c for k, c in classes.items() if c.stereotypeName != "SimpleAttribute"}


def enrich_classes_with_generalizations(classes, generalizations):
    for g in generalizations.values():
        # assert g.specific and g.general
        specific = g["specific"]
        general = g["general"]
        classes[specific].generalization.append(classes[general])
        classes[general].specialization.append(classes[specific])
    return classes


def enrich_classes_with_stereotypes(classes, all_elements, writer):
    # Tag classes with appliedStereotype
    for c in classes.values():
        if c.get("appliedStereotype"):
            # Figure out stereotype name through
            # Class.appliedStereotype.classifier.name
            instSpec = all_elements[c.appliedStereotype[0]]
            sType = all_elements[instSpec.classifier[0]]
            c.stereotypeName = sType.name
            writer.add_comment(
                f"class '{c.name}' has been stereotyped as '{c.stereotypeName}'"
            )

            def tag_children(me):
                for child in me.specialization:
                    child.stereotypeName = sType.name
                    writer.add_comment(
                        f"class '{child.name}' has been stereotyped as '{child.stereotypeName}' too"
                    )
                    tag_children(child)

            tag_children(c)
    return classes


def enrich_enumerations_with_values(enumerations, properties):
    for e in list(enumerations.values()):
        values = []
        for key in e["ownedAttribute"]:
            values.append(str(properties[key]["name"]))
        e.enumerates = tuple(values)
    return enumerations


def generate(filename, outfile=None, overridesfile=None):
    # parse the file
    all_elements = parse(filename)
    overrides = override.Overrides(overridesfile)
    writer = Writer(overrides)

    def resolve(val, attr):
        """Resolve references.
        """
        try:
            refs = val.references[attr]
        except KeyError:
            val.references[attr] = None
            return

        if isinstance(refs, type([])):
            unrefs = []
            for r in refs:
                unrefs.append(all_elements[r])
            val.references[attr] = unrefs
        else:
            val.references[attr] = all_elements[refs]

    # extract usable elements from all_elements. Some elements are given
    # some extra attributes.
    classes = {}
    enumerations = {}
    generalizations = {}
    associations = {}
    properties = {}
    operations = {}
    extensions = {}  # for identifying metaclasses
    for key, val in all_elements.items():
        # Find classes, *Kind (enumerations) are enumerations
        if isinstance(val, element):
            if val.type == "Class" and val.get("name"):
                if val["name"].endswith("Kind") or val["name"].endswith("Sort"):
                    enumerations[key] = val
                else:
                    # Metaclasses are removed later on (need to be checked
                    # via the Extension instances)
                    classes[key] = val
                    # Add extra properties for easy code generation:
                    val.specialization = []
                    val.generalization = []
                    val.stereotypeName = None
                    val.written = False
            elif val.type == "Generalization":
                generalizations[key] = val
            elif val.type == "Association":
                val.asAttribute = None
                associations[key] = val
            elif val.type == "Property":
                properties[key] = val
                resolve(val, "appliedStereotype")
                for st in val.appliedStereotype or []:
                    resolve(st, "slot")
                    for slot in st.slot or []:
                        resolve(slot, "value")
                        resolve(slot, "definingFeature")
                val.written = False
            elif val.type == "Operation":
                operations[key] = val
            elif val.type == "Extension":
                extensions[key] = val

    enumerations = enrich_enumerations_with_values(enumerations, properties)

    classes = enrich_classes_with_generalizations(classes, generalizations)
    classes = enrich_classes_with_stereotypes(classes, all_elements, writer)
    classes = filter_out_metaclasses(classes, extensions, all_elements)
    all_classes = classes
    classes = filter_out_simple_attributes(classes)

    for c in classes.values():
        writer.add_classdef(c)

    # create attributes and enumerations
    derivedattributes = {}
    for c in classes.values():
        for p in c.get("ownedAttribute") or []:
            a = properties.get(p)
            # set class_name, since add_attribute depends on it
            a.class_name = c["name"]
            if not a.get("association"):
                if overrides.derives(f"{a.class_name}.{a.name}"):
                    derivedattributes[a.name] = a
                else:
                    writer.add_attribute(a, enumerations)

    # create associations, derivedunions are held back
    derivedunions = {}  # indexed by name in stead of id
    redefines = []
    for a in list(associations.values()):
        ends = get_association_ends(a, properties, all_classes)

        for e1, e2 in ((ends[0], ends[1]), (ends[1], ends[0])):
            parse_association_end(e1, e2)

        for e1, e2 in ((ends[0], ends[1]), (ends[1], ends[0])):
            if a.asAttribute is not None:
                if a.asAttribute is e1 and e1.navigable:
                    writer.add_comment(
                        f"'{e2.type.name}.{e1.name}' is a simple attribute"
                    )
                    e1.class_name = e2.type.name
                    e1.typeValue = "str"

                    writer.add_attribute(e1, enumerations)
                    e1.written = True
                    e2.written = True
            elif e1.redefines:
                redefines.append(e1)
            elif e1.derived or overrides.derives(
                f"{e1.get('class_name')}.{e1.get('name')}"
            ):
                assert not derivedunions.get(e1.name), (
                    "%s.%s is already in derived union set in class %s"
                    % (e1.class_name, e1.name, derivedunions.get(e1.name).class_name)
                )
                derivedunions[e1.name] = e1
                e1.union = []
                e1.written = False
            elif e1.navigable:
                writer.add_association(e1, e2)

    # create derived unions, first link the association ends to the d
    for a in properties.values():
        for s in a.subsets or ():
            try:
                if a["type"] in classes:
                    derivedunions[s].union.append(a)
            except KeyError:
                msg(f"not a derived union: {a.class_name}.{s}")

    # TODO: We should do something smart here, since derived attributes (mostly)
    #       may depend on other derived attributes or associations.

    for d in list(derivedattributes.values()):
        writer.add_attribute(d)

    for d in list(derivedunions.values()):
        writer.add_derivedunion(d)

    for r in redefines or ():
        msg(f"redefining {r.redefines} -> {r.class_name}.{r.name}")
        writer.add_redefine(r)

    # create operations
    for c in classes.values():
        for p in c.get("ownedOperation") or ():
            o = operations.get(p)
            o.class_name = c["name"]
            writer.add_operation(o)

    writer.write(outfile, header)


if __name__ == "__main__":
    import doctest

    doctest.testmod()
    generate("uml2.gaphor")
