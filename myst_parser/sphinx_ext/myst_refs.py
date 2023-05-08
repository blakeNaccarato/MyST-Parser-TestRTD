"""A post-transform for overriding the behaviour of sphinx reference resolution.

This is applied to MyST type references only, such as ``[text](target)``,
and allows for nested syntax
"""
import os
from typing import Any, List, Optional, Tuple, cast

from docutils import nodes
from docutils.nodes import Element, document
from sphinx import addnodes, version_info
from sphinx.addnodes import pending_xref
from sphinx.domains.std import StandardDomain
from sphinx.locale import __
from sphinx.transforms.post_transforms import ReferencesResolver
from sphinx.util import docname_join, logging
from sphinx.util.nodes import clean_astext, make_refnode

from myst_parser._compat import findall

try:
    from sphinx.errors import NoUri
except ImportError:
    # sphinx < 2.1
    from sphinx.environment import NoUri  # type: ignore

logger = logging.getLogger(__name__)


class MystReferenceResolver(ReferencesResolver):
    """Resolves cross-references on doctrees.

    Overrides default sphinx implementation, to allow for nested syntax
    """

    default_priority = 9  # higher priority than ReferencesResolver (10)

    def run(self, **kwargs: Any) -> None:
        self.document: document
        for node in findall(self.document)(addnodes.pending_xref):
            if node["reftype"] != "myst":
                continue

            contnode = cast(nodes.TextElement, node[0].deepcopy())
            newnode = None

            target = node["reftarget"]
            refdoc = node.get("refdoc", self.env.docname)
            domain = None

            try:
                newnode = self.resolve_myst_ref(refdoc, node, contnode)
                if newnode is None:
                    # no new node found? try the missing-reference event
                    # but first we change the the reftype to 'any'
                    # this means it is picked up by extensions like intersphinx
                    node["reftype"] = "any"
                    try:
                        newnode = self.app.emit_firstresult(
                            "missing-reference",
                            self.env,
                            node,
                            contnode,
                            **(
                                {"allowed_exceptions": (NoUri,)}
                                if version_info[0] > 2
                                else {}
                            ),
                        )
                    finally:
                        node["reftype"] = "myst"
                # still not found? warn if node wishes to be warned about or
                # we are in nit-picky mode
                if newnode is None:
                    node["refdomain"] = ""
                    # TODO ideally we would override the warning message here,
                    # to show the [ref.myst] for suppressing warning
                    self.warn_missing_reference(
                        refdoc, node["reftype"], target, node, domain
                    )
            except NoUri:
                newnode = contnode

            node.replace_self(newnode or contnode)

    def resolve_myst_ref(
        self, refdoc: str, node: pending_xref, contnode: Element
    ) -> Element:
        """Resolve reference generated by the "myst" role; ``[text](reference)``.

        This builds on the sphinx ``any`` role to also resolve:

        - Document references with extensions; ``[text](./doc.md)``
        - Document references with anchors with anchors; ``[text](./doc.md#target)``
        - Nested syntax for explicit text with std:doc and std:ref;
          ``[**nested**](reference)``

        """
        target = node["reftarget"]  # type: str
        results = []  # type: List[Tuple[str, Element]]

        res_anchor = self._resolve_anchor(node, refdoc)
        if res_anchor:
            results.append(("std:doc", res_anchor))
        else:
            # if we've already found an anchored doc,
            # don't search in the std:ref/std:doc (leads to duplication)

            # resolve standard references
            res = self._resolve_ref_nested(node, refdoc)
            if res:
                results.append(("std:ref", res))

            # resolve doc names
            res = self._resolve_doc_nested(node, refdoc)
            if res:
                results.append(("std:doc", res))

        # get allowed domains for referencing
        ref_domains = self.env.config.myst_ref_domains

        assert self.app.builder

        # next resolve for any other standard reference objects
        if ref_domains is None or "std" in ref_domains:
            stddomain = cast(StandardDomain, self.env.get_domain("std"))
            for objtype in stddomain.object_types:
                key = (objtype, target)
                if objtype == "term":
                    key = (objtype, target.lower())
                if key in stddomain.objects:
                    docname, labelid = stddomain.objects[key]
                    domain_role = f"std:{stddomain.role_for_objtype(objtype)}"
                    ref_node = make_refnode(
                        self.app.builder, refdoc, docname, labelid, contnode
                    )
                    results.append((domain_role, ref_node))

        # finally resolve for any other type of allowed reference domain
        for domain in self.env.domains.values():
            if domain.name == "std":
                continue  # we did this one already
            if ref_domains is not None and domain.name not in ref_domains:
                continue
            try:
                results.extend(
                    domain.resolve_any_xref(
                        self.env, refdoc, self.app.builder, target, node, contnode
                    )
                )
            except NotImplementedError:
                # the domain doesn't yet support the new interface
                # we have to manually collect possible references (SLOW)
                if not (getattr(domain, "__module__", "").startswith("sphinx.")):
                    logger.warning(
                        f"Domain '{domain.__module__}::{domain.name}' has not "
                        "implemented a `resolve_any_xref` method [myst.domains]",
                        type="myst",
                        subtype="domains",
                        once=True,
                    )
                for role in domain.roles:
                    res = domain.resolve_xref(
                        self.env, refdoc, self.app.builder, role, target, node, contnode
                    )
                    if res and len(res) and isinstance(res[0], nodes.Element):
                        results.append((f"{domain.name}:{role}", res))

        # now, see how many matches we got...
        if not results:
            return None
        if len(results) > 1:

            def stringify(name, node):
                reftitle = node.get("reftitle", node.astext())
                return f":{name}:`{reftitle}`"

            candidates = " or ".join(stringify(name, role) for name, role in results)
            logger.warning(
                __(
                    f"more than one target found for 'myst' cross-reference {target}: "
                    f"could be {candidates} [myst.ref]"
                ),
                location=node,
                type="myst",
                subtype="ref",
            )

        res_role, newnode = results[0]
        # Override "myst" class with the actual role type to get the styling
        # approximately correct.
        res_domain = res_role.split(":")[0]
        if len(newnode) > 0 and isinstance(newnode[0], nodes.Element):
            newnode[0]["classes"] = newnode[0].get("classes", []) + [
                res_domain,
                res_role.replace(":", "-"),
            ]

        return newnode

    def _resolve_anchor(
        self, node: pending_xref, fromdocname: str
    ) -> Optional[Element]:
        """Resolve doc with anchor."""
        if self.env.config.myst_heading_anchors is None:
            # no target anchors will have been created, so we don't look for them
            return None
        target = node["reftarget"]  # type: str
        if "#" not in target:
            return None
        # the link may be a heading anchor; we need to first get the relative path
        rel_path, anchor = target.rsplit("#", 1)
        rel_path = os.path.normpath(rel_path)
        if rel_path == ".":
            # anchor in the same doc as the node
            doc_path = self.env.doc2path(node.get("refdoc", fromdocname), base=False)
        else:
            # anchor in a different doc from the node
            doc_path = os.path.normpath(
                os.path.join(node.get("refdoc", fromdocname), "..", rel_path)
            )
        return self._resolve_ref_nested(node, fromdocname, f"{doc_path}#{anchor}")

    def _resolve_ref_nested(
        self, node: pending_xref, fromdocname: str, target=None
    ) -> Optional[Element]:
        """This is the same as ``sphinx.domains.std._resolve_ref_xref``,
        but allows for nested syntax, rather than converting the inner node to raw text.
        """
        stddomain = cast(StandardDomain, self.env.get_domain("std"))
        target = target or node["reftarget"].lower()

        if node["refexplicit"]:
            # reference to anonymous label; the reference uses
            # the supplied link caption
            docname, labelid = stddomain.anonlabels.get(target, ("", ""))
            sectname = node.astext()
            innernode = nodes.inline(sectname, "")
            innernode.extend(node[0].children)
        else:
            # reference to named label; the final node will
            # contain the section name after the label
            docname, labelid, sectname = stddomain.labels.get(target, ("", "", ""))
            innernode = nodes.inline(sectname, sectname)

        if not docname:
            return None

        assert self.app.builder
        return make_refnode(self.app.builder, fromdocname, docname, labelid, innernode)

    def _resolve_doc_nested(
        self, node: pending_xref, fromdocname: str
    ) -> Optional[Element]:
        """This is the same as ``sphinx.domains.std._resolve_doc_xref``,
        but allows for nested syntax, rather than converting the inner node to raw text.

        It also allows for extensions on document names.
        """
        # directly reference to document by source name; can be absolute or relative
        refdoc = node.get("refdoc", fromdocname)
        docname = docname_join(refdoc, node["reftarget"])

        if (
            docname not in self.env.all_docs
            and os.path.splitext(docname)[1] in self.env.config.source_suffix
        ):
            docname = os.path.splitext(docname)[0]
        if docname not in self.env.all_docs:
            return None

        if node["refexplicit"]:
            # reference with explicit title
            caption = node.astext()
            innernode = nodes.inline(caption, "", classes=["doc"])
            innernode.extend(node[0].children)
        else:
            # TODO do we want nested syntax for titles?
            caption = clean_astext(self.env.titles[docname])
            innernode = nodes.inline(caption, caption, classes=["doc"])

        assert self.app.builder
        return make_refnode(self.app.builder, fromdocname, docname, "", innernode)
