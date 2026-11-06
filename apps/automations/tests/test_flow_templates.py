"""The flow template library: the operator's shelf and the customer's picker.

Three things are pinned here, in rough order of what breaks worst:

1. **The gate.** Every write is ``is_staff``, and a customer reaching one gets
   403 rather than a redirect. The shelf is global — there is no membership
   that could grant it — so a hole here is one customer editing what every
   other customer is offered.
2. **The id rewrite.** Applying a template copies its graph with fresh node
   ids. A collision does not fail loudly; it produces two nodes answering to
   one id, which the editor then moves together. ``instantiate_graph`` is the
   only place the two id spaces meet.
3. **The picker shows only what was published.** A half-written template in
   every customer's modal is the failure the ``is_published`` flag exists to
   prevent.

Asserts on status codes, JSON and stored state. No assertion on any rendered
sentence: the project defaults to pt-BR and copy is not a contract — the same
rule ``test_flow_new`` states.
"""

from __future__ import annotations

import json

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.automations import flow
from apps.automations.models import FlowTemplate
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


class InstantiateGraphTests(TestCase):
    """``flow.instantiate_graph`` — the copy that makes a template applicable."""

    def test_node_ids_are_rewritten(self):
        graph = {
            "nodes": [
                {"id": "trigger", "type": "trigger_comment", "x": 0, "y": 0, "data": {}},
                {"id": "dm", "type": "send_dm", "x": 300, "y": 0, "data": {"text": "hi"}},
            ],
            "edges": [{"from": "trigger", "to": "dm"}],
        }
        out = flow.instantiate_graph(graph)

        ids = [node["id"] for node in out["nodes"]]
        assert "trigger" not in ids
        assert "dm" not in ids
        assert len(set(ids)) == 2

    def test_edges_follow_the_rewrite(self):
        """An edge must point at the new ids, or it draws to the canvas origin."""
        graph = {
            "nodes": [
                {"id": "a", "type": "trigger_comment", "x": 0, "y": 0, "data": {}},
                {"id": "b", "type": "send_dm", "x": 300, "y": 0, "data": {}},
            ],
            "edges": [{"from": "a", "to": "b"}],
        }
        out = flow.instantiate_graph(graph)
        ids = {node["id"] for node in out["nodes"]}

        assert len(out["edges"]) == 1
        assert out["edges"][0]["from"] in ids
        assert out["edges"][0]["to"] in ids

    def test_branch_labels_survive(self):
        """``branch`` names an output port, not a node — rewriting it misroutes taps."""
        graph = {
            "nodes": [
                {"id": "c", "type": "condition", "x": 0, "y": 0, "data": {}},
                {"id": "y", "type": "send_dm", "x": 300, "y": 0, "data": {}},
            ],
            "edges": [{"from": "c", "to": "y", "branch": "true"}],
        }
        out = flow.instantiate_graph(graph)
        assert out["edges"][0]["branch"] == "true"

    def test_dangling_edges_are_dropped(self):
        graph = {
            "nodes": [{"id": "a", "type": "trigger_comment", "x": 0, "y": 0, "data": {}}],
            "edges": [{"from": "a", "to": "ghost"}],
        }
        assert flow.instantiate_graph(graph)["edges"] == []

    def test_positions_survive(self):
        """The layout is most of what a template is worth."""
        graph = {
            "nodes": [{"id": "a", "type": "send_dm", "x": 123, "y": 456, "data": {}}],
            "edges": [],
        }
        node = flow.instantiate_graph(graph)["nodes"][0]
        assert (node["x"], node["y"]) == (123, 456)

    def test_data_is_not_shared_with_the_source(self):
        """Editing the applied canvas must not reach back into the stored template."""
        graph = {
            "nodes": [{"id": "a", "type": "send_dm", "x": 0, "y": 0, "data": {"text": "original"}}],
            "edges": [],
        }
        out = flow.instantiate_graph(graph)
        out["nodes"][0]["data"]["text"] = "edited"

        assert graph["nodes"][0]["data"]["text"] == "original"

    def test_malformed_input_does_not_raise(self):
        """The source is a JSONField written by a browser."""
        for bad in (None, [], "nope", {}, {"nodes": "no"}, {"nodes": [{"no": "type"}]}):
            out = flow.instantiate_graph(bad)
            assert out == {"nodes": [], "edges": []}


class FlowTemplateTestCase(TestCase):
    """Shared actors: one operator, one ordinary member, one workspace.

    The shelf is emptied first. Migration ``0005`` seeds five published
    templates, so every count and every ``.get()`` below would otherwise be
    asserting against that content — and would change meaning the next time
    somebody adds a stock flow. These cases are about the mechanics (the gate,
    the id rewrite, what the picker filters), not about what ships on the
    shelf, so they start from nothing and put there exactly what they mean to
    assert on.
    """

    def setUp(self):
        FlowTemplate.objects.all().delete()
        self.operator = User.objects.create_user(
            email="operator@example.com",
            password="testpass123",
            is_staff=True,
            tos_accepted_at=timezone.now(),
        )
        self.member = User.objects.create_user(
            email="member@example.com",
            password="testpass123",
            tos_accepted_at=timezone.now(),
        )
        self.org = Organization.objects.create(name="Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="WS")
        OrgMembership.objects.create(
            user=self.member,
            organization=self.org,
            org_role=OrgMembership.OrgRole.OWNER,
        )
        WorkspaceMembership.objects.create(
            user=self.member,
            workspace=self.workspace,
            workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
        )

    def _template(self, **kwargs):
        defaults = {
            "name": "Guia grátis",
            "graph": flow.default_graph("dm"),
            "is_published": True,
        }
        return FlowTemplate.objects.create(**{**defaults, **kwargs})


class SeededShelfTests(TestCase):
    """What migration ``0005`` put on the shelf.

    Deliberately not a ``FlowTemplateTestCase`` subclass: that one empties the
    table, and these are about what the migration left in it. They assert on
    structure rather than on wording — the copy is the operator's to change
    from the admin screen the moment the deploy lands.
    """

    def test_the_shelf_is_not_empty(self):
        """An empty shelf after a deploy reads as a broken feature."""
        assert FlowTemplate.objects.exists()

    def test_every_stock_template_is_published(self):
        assert not FlowTemplate.objects.filter(is_published=False).exists()

    def test_every_stock_template_compiles_without_warnings(self):
        """A warning on a stock flow is the product shipping its own bug report."""
        for template in FlowTemplate.objects.all():
            compiled = flow.compile_graph(template.graph)
            assert compiled.warnings == [], f"{template.name}: {compiled.warnings}"

    def test_every_stock_template_can_act(self):
        """A template that sends nothing is a canvas nobody can turn on."""
        for template in FlowTemplate.objects.all():
            assert flow.compile_graph(template.graph).can_act, template.name

    def test_every_stock_template_has_exactly_one_trigger(self):
        for template in FlowTemplate.objects.all():
            triggers = [n for n in template.graph["nodes"] if n["type"] == "trigger_comment"]
            assert len(triggers) == 1, template.name

    def test_no_stock_template_points_at_a_local_image(self):
        """The flow this was lifted from carried one; in a template it 404s.

        An ``image_url`` on the shelf is a file in *somebody's* media library,
        which is exactly the URL every other customer's card fails to load.
        """
        for template in FlowTemplate.objects.all():
            for node in template.graph["nodes"]:
                assert not (node.get("data") or {}).get("image_url"), template.name

    def test_every_edge_joins_two_real_nodes(self):
        """A dangling edge draws a line to the canvas origin."""
        for template in FlowTemplate.objects.all():
            ids = {node["id"] for node in template.graph["nodes"]}
            for edge in template.graph["edges"]:
                assert edge["from"] in ids, template.name
                assert edge["to"] in ids, template.name

    def test_the_stock_templates_survive_being_applied(self):
        """The picker instantiates them; node count and wiring must come out whole."""
        for template in FlowTemplate.objects.all():
            applied = flow.instantiate_graph(template.graph)
            assert len(applied["nodes"]) == len(template.graph["nodes"]), template.name
            assert len(applied["edges"]) == len(template.graph["edges"]), template.name


class StaffAccessTests(FlowTemplateTestCase):
    """Who may touch the shelf. Every one of these is a 403, never a redirect."""

    def test_member_cannot_read_the_shelf(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("automations_staff:template_list"))
        assert response.status_code == 403

    def test_member_cannot_create(self):
        self.client.force_login(self.member)
        response = self.client.post(reverse("automations_staff:template_create"), {"name": "Mine"})
        assert response.status_code == 403
        assert FlowTemplate.objects.count() == 0

    def test_member_cannot_edit_or_save(self):
        template = self._template()
        self.client.force_login(self.member)

        edit = self.client.get(reverse("automations_staff:template_edit", kwargs={"template_id": template.id}))
        assert edit.status_code == 403

        save = self.client.post(
            reverse("automations_staff:template_save", kwargs={"template_id": template.id}),
            {"graph": '{"nodes": [], "edges": []}'},
        )
        assert save.status_code == 403

    def test_member_cannot_delete(self):
        template = self._template()
        self.client.force_login(self.member)
        response = self.client.post(reverse("automations_staff:template_delete", kwargs={"template_id": template.id}))
        assert response.status_code == 403
        assert FlowTemplate.objects.filter(id=template.id).exists()

    def test_anonymous_is_sent_to_log_in(self):
        """Not a 403: an anonymous visitor has somewhere to go first."""
        response = self.client.get(reverse("automations_staff:template_list"))
        assert response.status_code == 302

    def test_operator_reads_the_shelf(self):
        self._template()
        self.client.force_login(self.operator)
        response = self.client.get(reverse("automations_staff:template_list"))
        assert response.status_code == 200


class StaffShelfTests(FlowTemplateTestCase):
    """What the operator's writes actually store."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.operator)

    def test_create_seeds_a_canvas_and_opens_it(self):
        """A new template opens with a trigger already drawn, like a new flow."""
        response = self.client.post(reverse("automations_staff:template_create"), {"name": "Nova"})

        template = FlowTemplate.objects.get()
        assert template.name == "Nova"
        assert template.created_by == self.operator
        assert any(node["type"] == "trigger_comment" for node in template.graph["nodes"])
        self.assertRedirects(
            response,
            reverse("automations_staff:template_edit", kwargs={"template_id": template.id}),
        )

    def test_create_is_unpublished(self):
        """Nothing reaches a customer's picker until the operator says so."""
        self.client.post(reverse("automations_staff:template_create"), {"name": "Nova"})
        assert FlowTemplate.objects.get().is_published is False

    def test_create_refuses_a_blank_name(self):
        response = self.client.post(reverse("automations_staff:template_create"), {"name": "   "})
        assert FlowTemplate.objects.count() == 0
        self.assertRedirects(response, reverse("automations_staff:template_list"))

    def test_save_stores_the_graph(self):
        template = self._template()
        graph = {
            "nodes": [{"id": "n1", "type": "send_dm", "x": 10, "y": 20, "data": {"text": "oi"}}],
            "edges": [],
        }
        response = self.client.post(
            reverse("automations_staff:template_save", kwargs={"template_id": template.id}),
            {"graph": json.dumps(graph)},
        )

        assert response.status_code == 200
        assert response.json()["ok"] is True
        template.refresh_from_db()
        assert template.graph["nodes"][0]["data"]["text"] == "oi"

    def test_save_refuses_an_unreadable_graph(self):
        template = self._template()
        response = self.client.post(
            reverse("automations_staff:template_save", kwargs={"template_id": template.id}),
            {"graph": "not json"},
        )
        assert response.status_code == 400
        assert response.json()["ok"] is False

    def test_save_refuses_too_many_nodes(self):
        template = self._template()
        graph = {
            "nodes": [{"id": f"n{i}", "type": "send_dm", "x": 0, "y": 0, "data": {}} for i in range(100)],
            "edges": [],
        }
        response = self.client.post(
            reverse("automations_staff:template_save", kwargs={"template_id": template.id}),
            {"graph": json.dumps(graph)},
        )
        assert response.status_code == 400

    def test_details_save_the_card_fields(self):
        template = self._template(is_published=False)
        self.client.post(
            reverse("automations_staff:template_details", kwargs={"template_id": template.id}),
            {"name": "Renomeado", "description": "Uma linha", "position": "3", "is_published": "on"},
        )

        template.refresh_from_db()
        assert template.name == "Renomeado"
        assert template.description == "Uma linha"
        assert template.position == 3
        assert template.is_published is True

    def test_details_unpublish_by_omitting_the_checkbox(self):
        """An unchecked checkbox is absent from the POST, which must read as off."""
        template = self._template(is_published=True)
        self.client.post(
            reverse("automations_staff:template_details", kwargs={"template_id": template.id}),
            {"name": template.name, "position": "0"},
        )
        template.refresh_from_db()
        assert template.is_published is False

    def test_details_keep_the_text_when_the_position_is_nonsense(self):
        template = self._template(position=5)
        self.client.post(
            reverse("automations_staff:template_details", kwargs={"template_id": template.id}),
            {"name": "Mantido", "position": "abc"},
        )
        template.refresh_from_db()
        assert template.name == "Mantido"
        assert template.position == 5

    def test_duplicate_copies_the_graph_unpublished(self):
        source = self._template(is_published=True, description="Original")
        self.client.post(reverse("automations_staff:template_duplicate", kwargs={"template_id": source.id}))

        copy = FlowTemplate.objects.exclude(id=source.id).get()
        assert copy.is_published is False
        assert copy.description == "Original"
        assert len(copy.graph["nodes"]) == len(source.graph["nodes"])

    def test_duplicate_does_not_share_node_ids(self):
        source = self._template()
        self.client.post(reverse("automations_staff:template_duplicate", kwargs={"template_id": source.id}))

        copy = FlowTemplate.objects.exclude(id=source.id).get()
        source_ids = {node["id"] for node in source.graph["nodes"]}
        copy_ids = {node["id"] for node in copy.graph["nodes"]}
        assert source_ids.isdisjoint(copy_ids)

    def test_delete_removes_the_row(self):
        template = self._template()
        response = self.client.post(reverse("automations_staff:template_delete", kwargs={"template_id": template.id}))
        assert FlowTemplate.objects.count() == 0
        self.assertRedirects(response, reverse("automations_staff:template_list"))

    def test_the_canvas_opens_in_template_mode(self):
        """The builder is reused, and the flag is what tells the two apart."""
        template = self._template()
        response = self.client.get(reverse("automations_staff:template_edit", kwargs={"template_id": template.id}))
        assert response.status_code == 200
        assert response.context["template_mode"] is True
        assert response.context["flow_template"] == template
        # The post-dependent context is absent rather than blank: every
        # control that needs it is behind the flag.
        assert "platform_post" not in response.context


class PickerTests(FlowTemplateTestCase):
    """The customer's one endpoint."""

    def _url(self):
        return reverse("automations:template_picker", kwargs={"workspace_id": self.workspace.id})

    def test_member_reads_published_templates(self):
        self._template(name="Visível", is_published=True)
        self.client.force_login(self.member)

        response = self.client.get(self._url())
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert [item["name"] for item in payload["items"]] == ["Visível"]

    def test_drafts_are_not_offered(self):
        self._template(name="Rascunho", is_published=False)
        self.client.force_login(self.member)

        assert self.client.get(self._url()).json()["items"] == []

    def test_the_graph_travels_with_each_item(self):
        """The picker applies on click; a second round trip there is a spinner."""
        self._template()
        self.client.force_login(self.member)

        item = self.client.get(self._url()).json()["items"][0]
        assert item["graph"]["nodes"]
        assert item["node_count"] == len(item["graph"]["nodes"])

    def test_the_served_graph_has_fresh_ids(self):
        """Collisions with the ids already on the author's canvas are the bug."""
        template = self._template(
            graph={
                "nodes": [{"id": "trigger", "type": "trigger_comment", "x": 0, "y": 0, "data": {}}],
                "edges": [],
            }
        )
        self.client.force_login(self.member)

        item = self.client.get(self._url()).json()["items"][0]
        assert item["graph"]["nodes"][0]["id"] != "trigger"
        # The stored row is untouched — the rewrite happens on the way out.
        template.refresh_from_db()
        assert template.graph["nodes"][0]["id"] == "trigger"

    def test_the_shelf_is_the_same_for_every_workspace(self):
        """It is the product's library, not a workspace's."""
        other = Workspace.objects.create(organization=self.org, name="WS2")
        WorkspaceMembership.objects.create(
            user=self.member,
            workspace=other,
            workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
        )
        self._template(name="Compartilhado")
        self.client.force_login(self.member)

        first = self.client.get(self._url()).json()["items"]
        second = self.client.get(reverse("automations:template_picker", kwargs={"workspace_id": other.id})).json()[
            "items"
        ]
        assert [i["name"] for i in first] == [i["name"] for i in second] == ["Compartilhado"]

    def test_a_non_member_is_refused(self):
        """``require_permission`` resolves a membership; an outsider has none."""
        outsider = User.objects.create_user(
            email="outsider@example.com",
            password="testpass123",
            tos_accepted_at=timezone.now(),
        )
        self._template()
        self.client.force_login(outsider)

        assert self.client.get(self._url()).status_code in (403, 404)

    def test_the_shelf_order_is_the_position(self):
        self._template(name="Segundo", position=10)
        self._template(name="Primeiro", position=1)
        self.client.force_login(self.member)

        names = [item["name"] for item in self.client.get(self._url()).json()["items"]]
        assert names == ["Primeiro", "Segundo"]
