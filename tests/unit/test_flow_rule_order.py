"""Regression tests: order-sensitive Flow metadata must not normalize to equal.

Flow evaluates <decisions> rules in document order (first match wins), and
screen <fields> render in document order. Reordering these elements is a real
behavioral change, not formatting noise — the normalizer must keep their order.

Reference: Salesforce Metadata API Developer Guide, FlowDecision /
FlowWaitEvent / FlowScreenField — rule and field lists are evaluated in
listed order (https://resources.docs.salesforce.com/latest/latest/en-us/sfdc/pdf/api_meta.pdf).
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from xml_normalizer import normalize_xml, xml_semantically_equal

import env_compare  # loaded by conftest.py


SF_NS = "http://soap.sforce.com/2006/04/metadata"


def _flow(body: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Flow xmlns="{SF_NS}">\n'
        f'    <apiVersion>59.0</apiVersion>\n'
        f'{body}'
        f'    <status>Active</status>\n'
        f'</Flow>\n'
    )


def _rule(name: str, target: str, op: str, value: str) -> str:
    """A decision outcome; overlapping conditions make order semantic:
    for a record matching both rules, the FIRST listed outcome wins."""
    return (
        f'        <rules>\n'
        f'            <name>{name}</name>\n'
        f'            <conditionLogic>and</conditionLogic>\n'
        f'            <conditions>\n'
        f'                <leftValueReference>{{!record.Amount}}</leftValueReference>\n'
        f'                <operator>{op}</operator>\n'
        f'                <rightValue>\n'
        f'                    <numberValue>{value}</numberValue>\n'
        f'                </rightValue>\n'
        f'            </conditions>\n'
        f'            <connector>\n'
        f'                <targetReference>{target}</targetReference>\n'
        f'            </connector>\n'
        f'            <label>{name}</label>\n'
        f'        </rules>\n'
    )


def _decision(rules_xml: str) -> str:
    return (
        f'    <decisions>\n'
        f'        <name>Route_Record</name>\n'
        f'        <label>Route Record</label>\n'
        f'        <locationX>100</locationX>\n'
        f'        <locationY>200</locationY>\n'
        f'        <defaultConnector>\n'
        f'            <targetReference>End_Screen</targetReference>\n'
        f'        </defaultConnector>\n'
        f'        <defaultConnectorLabel>Default Outcome</defaultConnectorLabel>\n'
        f'{rules_xml}'
        f'    </decisions>\n'
    )


# Two outcomes with overlapping conditions — both match Amount=20000, so the
# first listed rule wins. Swapping their order changes runtime behavior.
RULE_HIGH = _rule("High_Value", "High_Value_Screen", "GreaterThan", "10000")
RULE_LOW = _rule("Low_Value", "Low_Value_Screen", "GreaterThan", "5000")

FLOW_HIGH_FIRST = _flow(_decision(RULE_HIGH + RULE_LOW))
FLOW_LOW_FIRST = _flow(_decision(RULE_LOW + RULE_HIGH))


class TestFlowDecisionRuleOrder:
    def test_reordered_decision_rules_not_equal(self):
        """RED: 'rules' is globally keyed by fullName (Workflow inheritance), so
        reordered Flow outcomes currently normalize to identical output."""
        assert normalize_xml(FLOW_HIGH_FIRST) != normalize_xml(FLOW_LOW_FIRST)

    def test_identical_rule_order_normalizes_equal(self):
        """Formatting-only churn inside the same decision still normalizes away."""
        pretty = FLOW_HIGH_FIRST.replace("        <rules>", "\n        <rules>")
        assert normalize_xml(FLOW_HIGH_FIRST) == normalize_xml(pretty)

    def test_semantically_equal_files(self, tmp_path):
        a = tmp_path / "a.flow-meta.xml"
        b = tmp_path / "b.flow-meta.xml"
        a.write_text(FLOW_HIGH_FIRST)
        b.write_text(FLOW_LOW_FIRST)
        assert not xml_semantically_equal(a, b)

    def test_tree_compare_detects_reordered_rules(self, tmp_path):
        """End-to-end: two trees whose only difference is Flow rule order must
        produce one difference — not one 'identical normalized' file."""
        from retrieved_folder_compare import compare_trees

        left = tmp_path / "left"
        right = tmp_path / "right"
        (left / "flows").mkdir(parents=True)
        (right / "flows").mkdir(parents=True)
        (left / "flows" / "Route.flow-meta.xml").write_text(FLOW_HIGH_FIRST)
        (right / "flows" / "Route.flow-meta.xml").write_text(FLOW_LOW_FIRST)
        result = compare_trees(left, right)
        assert len(result.differ_pairs) == 1
        assert result.identical_count == 0
        assert not result.identical_normalized_pairs

    def test_normalize_idempotent_on_flow(self):
        once = normalize_xml(FLOW_HIGH_FIRST)
        assert normalize_xml(once) == once


class TestFlowConditionsNumberedLogic:
    """<conditions> are referenced by position in <conditionLogic> strings
    (e.g. '(1 AND 2) OR 3') — swapping them changes the evaluated formula."""

    def _rule_with_logic(self, conds: str, logic: str) -> str:
        return (
            f'        <rules>\n'
            f'            <name>Mixed</name>\n'
            f'            <conditionLogic>{logic}</conditionLogic>\n'
            f'{conds}'
            f'            <connector>\n'
            f'                <targetReference>T</targetReference>\n'
            f'            </connector>\n'
            f'            <label>Mixed</label>\n'
            f'        </rules>\n'
        )

    @staticmethod
    def _cond(field: str, op: str, value: str) -> str:
        return (
            f'            <conditions>\n'
            f'                <leftValueReference>{{!record.{field}}}</leftValueReference>\n'
            f'                <operator>{op}</operator>\n'
            f'                <rightValue>\n'
            f'                    <stringValue>{value}</stringValue>\n'
            f'                </rightValue>\n'
            f'            </conditions>\n'
        )

    def test_swapped_conditions_detected(self):
        c1 = self._cond("Type", "EqualTo", "Customer")
        c2 = self._cond("Industry", "EqualTo", "Banking")
        c3 = self._cond("Rating", "EqualTo", "Hot")
        flow_a = _flow(_decision(self._rule_with_logic(c1 + c2 + c3, "(1 AND 2) OR 3")))
        flow_b = _flow(_decision(self._rule_with_logic(c2 + c1 + c3, "(1 AND 2) OR 3")))
        assert normalize_xml(flow_a) != normalize_xml(flow_b)


class TestWorkflowCriteriaItemsOrder:
    """R1: <criteriaItems> under a numbered <booleanFilter> are positional —
    swapping items changes the evaluated expression. Applies to Workflow
    rules and criteria-based sharing rules alike."""

    @staticmethod
    def _workflow(order) -> str:
        items = "".join(
            "<criteriaItems>"
            f"<field>Account.{f}__c</field><operation>equals</operation>"
            "<value>true</value></criteriaItems>"
            for f in order
        )
        return (
            f'<Workflow xmlns="{SF_NS}"><rules><fullName>Review</fullName>'
            "<active>true</active><booleanFilter>1 AND (2 OR 3)</booleanFilter>"
            f"{items}<triggerType>onCreateOnly</triggerType></rules></Workflow>"
        )

    @staticmethod
    def _sharing(order) -> str:
        items = "".join(
            "<criteriaItems>"
            f"<field>{f}__c</field><operation>equals</operation>"
            "<value>true</value></criteriaItems>"
            for f in order
        )
        return (
            f'<Account xmlns="{SF_NS}"><sharingCriteriaRules>'
            "<fullName>ShareReview</fullName><accessLevel>Read</accessLevel>"
            "<booleanFilter>1 AND (2 OR 3)</booleanFilter>"
            f"{items}<sharedTo><group>AllUsers</group></sharedTo>"
            "</sharingCriteriaRules></Account>"
        )

    def test_swapped_workflow_criteria_not_equal(self):
        """A AND (B OR C) != B AND (A OR C): for A=1,B=0,C=1 the first
        expression is true and the second false."""
        assert normalize_xml(
            self._workflow(["A", "B", "C"])
        ) != normalize_xml(self._workflow(["B", "A", "C"]))

    def test_swapped_sharing_criteria_not_equal(self):
        assert normalize_xml(
            self._sharing(["A", "B", "C"])
        ) != normalize_xml(self._sharing(["B", "A", "C"]))

    def test_same_criteria_order_normalizes_equal(self):
        """Whitespace-only churn with unchanged order still normalizes away."""
        a = self._workflow(["A", "B", "C"])
        b = a.replace("<criteriaItems>", "\n<criteriaItems>")
        assert normalize_xml(a) == normalize_xml(b)

    def test_tree_compare_detects_criteria_reorder(self, tmp_path):
        from retrieved_folder_compare import compare_trees

        left, right = tmp_path / "left", tmp_path / "right"
        for root, order in ((left, "ABC"), (right, "BAC")):
            (root / "workflows").mkdir(parents=True)
            (root / "workflows" / "Account.workflow-meta.xml").write_text(
                self._workflow(order)
            )
        result = compare_trees(left, right)
        assert len(result.differ_pairs) == 1
        assert not result.identical_normalized_pairs


class TestFlowScreenFieldOrder:
    """Screen <fields> render in document order — reordering changes the form."""

    @staticmethod
    def _screen(fields_xml: str) -> str:
        return (
            f'    <screens>\n'
            f'        <name>Input_Screen</name>\n'
            f'        <label>Input Screen</label>\n'
            f'        <locationX>300</locationX>\n'
            f'        <locationY>400</locationY>\n'
            f'{fields_xml}'
            f'    </screens>\n'
        )

    @staticmethod
    def _screen_field(name: str) -> str:
        return (
            f'        <fields>\n'
            f'            <name>{name}</name>\n'
            f'            <dataType>String</dataType>\n'
            f'            <fieldText>{name}</fieldText>\n'
            f'            <fieldType>InputField</fieldType>\n'
            f'            <isRequired>false</isRequired>\n'
            f'        </fields>\n'
        )

    def test_screen_field_order_is_semantic(self):
        """'fields' is globally keyed by <name> for CustomObjectTranslation; under
        a Flow <screens> element the order is the rendered form order."""
        f_a = self._screen_field("First_Name")
        f_b = self._screen_field("Last_Name")
        flow_a = _flow(self._screen(f_a + f_b))
        flow_b = _flow(self._screen(f_b + f_a))
        assert normalize_xml(flow_a) != normalize_xml(flow_b)


class TestFlowWaitEventOrder:
    """Wait events are evaluated in order — first satisfied event resumes."""

    @staticmethod
    def _wait(events_xml: str) -> str:
        return (
            f'    <waits>\n'
            f'        <name>Wait_For_Response</name>\n'
            f'        <label>Wait For Response</label>\n'
            f'        <locationX>500</locationX>\n'
            f'        <locationY>600</locationY>\n'
            f'{events_xml}'
            f'    </waits>\n'
        )

    @staticmethod
    def _wait_event(name: str, target: str) -> str:
        return (
            f'        <waitEvents>\n'
            f'            <name>{name}</name>\n'
            f'            <conditionLogic>and</conditionLogic>\n'
            f'            <connector>\n'
            f'                <targetReference>{target}</targetReference>\n'
            f'            </connector>\n'
            f'            <eventType>AlarmRefOrEventDefRef</eventType>\n'
            f'            <label>{name}</label>\n'
            f'        </waitEvents>\n'
        )

    def test_wait_event_order_is_semantic(self):
        e1 = self._wait_event("Responded", "After_Response")
        e2 = self._wait_event("Timed_Out", "After_Timeout")
        flow_a = _flow(self._wait(e1 + e2))
        flow_b = _flow(self._wait(e2 + e1))
        assert normalize_xml(flow_a) != normalize_xml(flow_b)


class TestFlowCliDiff(unittest.TestCase):
    """CLI-level: a rule-order-only Flow change must trip --fail-on-diff."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.left = self.tmp_path / "left"
        self.right = self.tmp_path / "right"
        (self.left / "flows").mkdir(parents=True)
        (self.right / "flows").mkdir(parents=True)
        (self.left / "flows" / "Route.flow-meta.xml").write_text(FLOW_HIGH_FIRST)
        (self.right / "flows" / "Route.flow-meta.xml").write_text(FLOW_LOW_FIRST)

        self._orig = {
            name: getattr(env_compare, name)
            for name in ("STATE_DIR", "INDEX_PATH", "COMPARISON_INDEX_PATH",
                         "STORAGE_ROOT", "PROJECT_ROOT")
        }
        env_compare.STORAGE_ROOT = self.tmp_path
        env_compare.PROJECT_ROOT = self.tmp_path
        env_compare.STATE_DIR = self.tmp_path
        env_compare.INDEX_PATH = self.tmp_path / "snapshots.json"
        env_compare.COMPARISON_INDEX_PATH = self.tmp_path / "comparisons.json"

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(env_compare, name, value)
        self.tmp.cleanup()

    def test_flow_rule_order_diff_exits_nonzero_with_fail_on_diff(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = env_compare.run_diff(
                str(self.left), str(self.right),
                as_json=False, fail_on_diff=True,
            )
        self.assertEqual(rc, 1)
