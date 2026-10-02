#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Generate the AWS reference architecture diagram for this project.

Written as a generator rather than hand-authored XML because the grid is arithmetic:
every icon needs the same long style string with one field changed, and every edge that
leaves its own zone has to agree on a routing lane. Hand-editing that is how you end up
with a missing `fontFamily` on cell nine and a lane 4px off on edge twenty.

The diagram is organised as **zones on one spine**, not as a grid. Everything in the
request path sits on row 0 and reads strictly left to right; whatever a stage depends on
hangs directly beneath it; the two bands along the bottom are what the system writes
down. That replaced a 7x3 grid plus a ten-step sidebar legend, which was accurate but
unreadable -- the rows were spatial rather than logical, so the eye had no order to
follow, and the legend ended up carrying the explanation the picture should carry itself.
Each zone's one-line subtitle now does that job instead.

Re-run after changing any stack topology, then re-export the PNG with draw.io
desktop (``brew install --cask drawio``); ``-e`` embeds the diagram's XML in the PNG,
so the picture in the README is also the editable source:

    infra/.venv/bin/python scripts/build_architecture_diagram.py
    drawio -x -f png -e -b 10 -s 2 -o docs/hotel-operations-agents-architecture.drawio.png docs/hotel-operations-agents-architecture.drawio
"""

from __future__ import annotations

import html
from pathlib import Path

class Element:
    """A write-only XML element: a tag, ordered attributes, children.

    This module used to build its tree with the standard library's ElementTree. It
    never parsed anything, but importing the stdlib XML package at all trips the XXE
    rules (semgrep use-defused-xml, bandit B405), and defusedxml -- the usual answer --
    hardens *parsers* and offers nothing for building documents. So the module no
    longer imports an XML library. This class and :func:`serialise` produce output
    byte-identical to what ElementTree's indent + tostring produced, which was verified
    by diffing the generated .drawio before and after the change.
    """

    # Positional-only, as in ElementTree: mxGraph has an attribute called ``parent``,
    # and a keyword parameter of that name would swallow it.
    def __init__(self, tag: str, attrib: dict | None = None, /, **extra: str):
        self.tag = tag
        self.attrib = {**(attrib or {}), **extra}
        self.children: list[Element] = []


def SubElement(  # noqa: N802 - mirrors the API it replaced, so call sites read the same
    parent_element: Element, tag: str, attrib: dict | None = None, /, **extra: str
) -> Element:
    child = Element(tag, attrib, **extra)
    parent_element.children.append(child)
    return child


def _escape_attribute(value: str) -> str:
    """The escaping ElementTree applies to attribute values."""
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("\r", "&#13;").replace("\n", "&#10;")
        .replace("\t", "&#09;")
    )


def serialise(element: Element, depth: int = 0) -> str:
    """Two-space indented, with self-closing empty elements: ElementTree's layout."""
    pad = "  " * depth
    attrs = "".join(f' {k}="{_escape_attribute(str(v))}"' for k, v in element.attrib.items())
    if not element.children:
        return f"{pad}<{element.tag}{attrs} />"
    inner = "\n".join(serialise(child, depth + 1) for child in element.children)
    return f"{pad}<{element.tag}{attrs}>\n{inner}\n{pad}</{element.tag}>"


REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "docs" / "hotel-operations-agents-architecture.drawio"

FONT = "fontFamily=Helvetica;"

# Category tints from the AWS reference-architecture style guide.
CATEGORY = {
    "compute": ("#FFF2E8", "#ED7100"),
    "database": ("#F5E6F7", "#C925D1"),
    "network": ("#EDE7F6", "#8C4FFF"),
    "storage": ("#E8F5E9", "#3F8624"),
    "integration": ("#FCE4EC", "#E7157B"),
    "aiml": ("#E0F2F1", "#01A88D"),
    "security": ("#FFEBEE", "#DD344C"),
}

# --------------------------------------------------------------------------- #
# Geometry. All coordinates are relative to the AWS Cloud group.
# --------------------------------------------------------------------------- #
BOX, ICON = 120, 48
ROW = [140, 320, 500]        # row 0 is the spine; rows 1 and 2 hang beneath it
ZONE_Y, ZONE_H = 70, 580     # 70px of caption clearance above row 0
BAND_Y, BAND_H, BAND_ROW = 740, 220, 810   # same 70px caption clearance as a zone

# Two lanes in the gap between the zones and the bands. Nested rather than crossing:
# whichever edge travels further right takes the lower lane.
LANE_A, LANE_B = 676, 704

CLOUD = dict(x=240, y=170, w=1910, h=1000)
PAGE_W = CLOUD["x"] + CLOUD["w"] + 60
PAGE_H = CLOUD["y"] + CLOUD["h"] + 60

ZONE_STROKE = "#7D8B8C"
EXTERNAL_STROKE = "#00A4A6"


def cell(root, cid, style, value, parent, geom, vertex="1", **attrs):
    node = SubElement(root, "mxCell", id=cid, value=value, style=style,
                         parent=parent, vertex=vertex, **attrs)
    SubElement(node, "mxGeometry", {str(k): str(v) for k, v in geom.items()},
                  **{"as": "geometry"})
    return node


def caption(title: str, sub: str) -> str:
    """A zone caption: bold name, then the one line that used to be a legend entry."""
    out = f"&lt;b&gt;{html.escape(title)}&lt;/b&gt;"
    if sub:
        out += ("&lt;div&gt;&lt;font style=&quot;font-size: 11px;&quot;&gt;&lt;i&gt;"
                f"{html.escape(sub)}&lt;/i&gt;&lt;/font&gt;&lt;/div&gt;")
    return out


def zone(root, key, *, n, title, sub, x, w, y=ZONE_Y, h=ZONE_H, external=False):
    """A logical boundary, drawn as decoration only.

    `container=0` on purpose: the services inside stay children of the AWS Cloud group
    with absolute coordinates, so edges cross zone walls without the nesting depth that
    breaks draw.io's orthogonal router.
    """
    stroke = EXTERNAL_STROKE if external else ZONE_STROKE
    cell(root, f"zone-{key}",
         f"rounded=1;whiteSpace=wrap;html=1;fillColor=none;strokeColor={stroke};"
         "dashed=1;dashPattern=8 6;strokeWidth=1.5;verticalAlign=top;align=left;"
         f"spacingLeft=14;spacingTop=6;fontSize=14;fontColor={stroke};{FONT}"
         "container=0;pointerEvents=0;collapsible=0;",
         caption(f"{n} · {title}" if n else title, sub),
         "aws-cloud", dict(x=x, y=y, width=w, height=h))


def service(root, key, *, category, shape, label, sub, group_label, x, y):
    """A 120x120 category container holding one 48x48 service icon.

    The container carries the functional category and the icon carries the service name.
    The style guide is emphatic about that split, and it is what makes the diagram
    scannable: read the category to find the layer, then the icon inside it.
    """
    tint, stroke = CATEGORY[category]
    cell(root, f"grp-{key}",
         f"fillColor={tint};strokeColor={stroke};rounded=1;whiteSpace=wrap;html=1;"
         f"verticalAlign=top;fontStyle=1;fontSize=12;fontColor={stroke};{FONT}"
         "container=1;collapsible=0;shadow=1;strokeWidth=1.5;",
         group_label, "aws-cloud", dict(x=x, y=y, width=BOX, height=BOX))

    value = html.escape(label)
    if sub:
        value += f"&lt;div&gt;&lt;i&gt;{html.escape(sub)}&lt;/i&gt;&lt;/div&gt;"
    cell(root, key,
         "sketch=0;points=[[0,0,0],[0.25,0,0],[0.5,0,0],[0.75,0,0],[1,0,0],[0,1,0],"
         "[0.25,1,0],[0.5,1,0],[0.75,1,0],[1,1,0],[0,0.25,0],[0,0.5,0],[0,0.75,0],"
         "[1,0.25,0],[1,0.5,0],[1,0.75,0]];outlineConnect=0;fontColor=#232F3E;"
         f"fillColor={stroke};strokeColor=#ffffff;dashed=0;verticalLabelPosition=bottom;"
         "verticalAlign=top;align=center;html=1;fontSize=10;fontStyle=0;aspect=fixed;"
         f"shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.{shape};{FONT}shadow=1;",
         value, f"grp-{key}", dict(x=36, y=30, width=ICON, height=ICON))


def edge(root, eid, source, target, *, label="", exit_xy=None, entry_xy=None,
         waypoints=None, dashed=False, label_x=0.0, label_y=0):
    style = ("edgeStyle=orthogonalEdgeStyle;html=1;endArrow=block;elbow=vertical;"
             f"startArrow=none;endFill=1;strokeColor=#545B64;rounded=0;{FONT}"
             "jettySize=auto;strokeWidth=1.5;")
    if dashed:
        style += "dashed=1;"
    if exit_xy:
        style += f"exitX={exit_xy[0]};exitY={exit_xy[1]};exitDx=0;exitDy=0;"
    if entry_xy:
        style += f"entryX={entry_xy[0]};entryY={entry_xy[1]};entryDx=0;entryDy=0;"

    node = SubElement(root, "mxCell", id=eid, value="", style=style, parent="1",
                         edge="1", source=source, target=target)
    geom = SubElement(node, "mxGeometry", {"relative": "1"}, **{"as": "geometry"})
    if waypoints:
        array = SubElement(geom, "Array", **{"as": "points"})
        for wx, wy in waypoints:
            SubElement(array, "mxPoint", x=str(wx), y=str(wy))

    if label:
        lab = SubElement(
            root, "mxCell", id=f"{eid}-label", value=html.escape(label),
            style="edgeLabel;html=1;align=center;verticalAlign=middle;resizable=0;"
                  f"points=[];labelBackgroundColor=none;fontSize=11;{FONT}",
            parent=eid, vertex="1", connectable="0")
        lg = SubElement(lab, "mxGeometry",
                           {"relative": "1", "x": str(label_x), "y": str(label_y)},
                           **{"as": "geometry"})
        SubElement(lg, "mxPoint", **{"as": "offset"})


# --------------------------------------------------------------------------- #
# The diagram itself.
# --------------------------------------------------------------------------- #
# Zone x origin and width. Zone 1 and 2 are two columns wide; the rest are one.
Z = {
    "console":  (40, 380),
    "invoke":   (490, 380),
    "agents":   (940, 180),
    "toolplane": (1190, 180),
    "targets":  (1440, 180),
    "platform": (1690, 180),
}
# Icon x positions inside each zone.
C = {"console_a": 70, "console_b": 250, "invoke_a": 520, "invoke_b": 700,
     "agents": 970, "toolplane": 1220, "targets": 1470, "platform": 1720}
# The two bottom bands.
BAND = {"grading": (940, 380), "state": (1390, 380)}
B = {"logs": 970, "evals": 1170, "approvals": 1420, "decisions": 1620}


def build() -> Element:
    mxfile = Element("mxfile", host="Electron", version="29.6.1")
    diagram = SubElement(mxfile, "diagram", name="Page-1", id="diagram-1")
    model = SubElement(
        diagram, "mxGraphModel", dx="1400", dy="900", grid="0", gridSize="10",
        guides="1", tooltips="1", connect="1", arrows="1", fold="1", page="0",
        pageScale="1", pageWidth=str(PAGE_W), pageHeight=str(PAGE_H), math="0",
        shadow="0")
    root = SubElement(model, "root")
    SubElement(root, "mxCell", id="0")
    SubElement(root, "mxCell", id="1", parent="0")

    # ---- title -------------------------------------------------------------- #
    cell(root, "title-group", f"group;{FONT}", "", "1",
         dict(x=50, y=30, width=PAGE_W - 100, height=83), connectable="0")
    cell(root, "title-text",
         "text;html=1;resizable=1;points=[];autosize=1;align=left;verticalAlign=top;"
         f"spacingTop=-4;fontSize=30;fontStyle=1;{FONT}",
         "Hotel Operations Agents on Amazon Bedrock AgentCore", "title-group",
         dict(width=1100, height=42))
    cell(root, "subtitle-text",
         "text;html=1;resizable=0;points=[];autosize=1;align=left;verticalAlign=top;"
         f"spacingTop=-4;fontSize=16;{FONT}",
         "One orchestrator and five specialist agents reach a live hospitality platform "
         "only through its REST APIs, with money movement gated outside the model",
         "title-group", dict(x=5, y=40, width=1400, height=25))
    cell(root, "title-separator",
         f"line;strokeWidth=2;html=1;fontSize=14;strokeColor=#FF9900;{FONT}", "",
         "title-group", dict(x=5, y=70, width=PAGE_W - 110, height=10))

    # ---- external actor ----------------------------------------------------- #
    cell(root, "users-container",
         "fillColor=#f5f5f5;strokeColor=light-dark(#666666,#D4D4D4);rounded=1;"
         "whiteSpace=wrap;html=1;verticalAlign=top;fontStyle=1;fontSize=12;"
         f"fontColor=#333333;{FONT}container=1;collapsible=0;shadow=1;strokeWidth=1;",
         "Hotel staff", "1", dict(x=90, y=CLOUD["y"] + ROW[0], width=107, height=98))
    cell(root, "users-icon",
         "sketch=0;points=[[0,0,0],[0.25,0,0],[0.5,0,0],[0.75,0,0],[1,0,0],[0,1,0],"
         "[0.25,1,0],[0.5,1,0],[0.75,1,0],[1,1,0],[0,0.25,0],[0,0.5,0],[0,0.75,0],"
         "[1,0.25,0],[1,0.5,0],[1,0.75,0]];outlineConnect=0;fontColor=#232F3E;"
         "fillColor=#232F3D;strokeColor=#ffffff;dashed=0;verticalLabelPosition=bottom;"
         "verticalAlign=top;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;"
         f"shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.users;{FONT}",
         "", "users-container", dict(x=30, y=30, width=ICON, height=ICON))

    # ---- AWS Cloud ---------------------------------------------------------- #
    cell(root, "aws-cloud",
         "points=[[0,0],[0.25,0],[0.5,0],[0.75,0],[1,0],[1,0.25],[1,0.5],[1,0.75],"
         "[1,1],[0.75,1],[0.5,1],[0.25,1],[0,1],[0,0.75],[0,0.5],[0,0.25]];"
         "outlineConnect=0;gradientColor=none;html=1;whiteSpace=wrap;fontSize=12;"
         "fontStyle=0;shape=mxgraph.aws4.group;grIcon=mxgraph.aws4.group_aws_cloud;"
         "strokeColor=#232F3E;fillColor=light-dark(#232F3E0D,#232F3E0D);fillStyle=auto;"
         f"verticalAlign=top;align=left;spacingLeft=30;fontColor=#232F3E;dashed=0;{FONT}"
         "container=1;pointerEvents=0;collapsible=0;recursiveResize=0;",
         "AWS Cloud &#183; us-east-1 &#183; seven CDK stacks, all hotel-ops-agent-*", "1",
         dict(x=CLOUD["x"], y=CLOUD["y"], width=CLOUD["w"], height=CLOUD["h"]))

    # ---- zones -------------------------------------------------------------- #
    z = lambda k, **kw: zone(root, k, x=Z[k][0], w=Z[k][1], **kw)  # noqa: E731
    z("console", n=1, title="Ops console",
      sub="existing hotel Cognito accounts · same origin, no CORS")
    z("invoke", n=2, title="Invocation",
      sub="chat, four cadences and three event rules share one durable path")
    z("agents", n=3, title="Agent plane", sub="one runtime, six agents")
    z("toolplane", n=4, title="Tool plane", sub="the Tier-2 gate lives here")
    z("targets", n=5, title="Tool targets", sub="one identity per domain")
    z("platform", n=6, title="Hospitality platform", sub="separate repo, unmodified",
      external=True)

    zone(root, "grading", n=None, title="Observability & grading",
         sub="graded on live traffic, not on a fixture set",
         x=BAND["grading"][0], w=BAND["grading"][1], y=BAND_Y, h=BAND_H)
    zone(root, "state", n=None, title="State & audit",
         sub="an approval binds action, target and amount",
         x=BAND["state"][0], w=BAND["state"][1], y=BAND_Y, h=BAND_H)

    # ---- services ----------------------------------------------------------- #
    s = lambda **kw: service(root, **kw)  # noqa: E731 - local alias, reads better

    # Zone 1 -- the console. Spine: CloudFront then API Gateway.
    s(key="cloudfront", category="network", shape="cloudfront", label="CloudFront",
      sub="one origin for app + API", group_label="Edge",
      x=C["console_a"], y=ROW[0])
    s(key="s3", category="storage", shape="s3", label="S3", sub="console bundle, OAC",
      group_label="Static hosting", x=C["console_a"], y=ROW[1])
    s(key="apigw", category="network", shape="api_gateway", label="API Gateway",
      sub="chat · approvals · runs", group_label="Console API",
      x=C["console_b"], y=ROW[0])
    s(key="cognito", category="security", shape="cognito", label="Cognito",
      sub="the platform's own pool", group_label="Identity",
      x=C["console_b"], y=ROW[1])

    # Zone 2 -- invocation. Both unattended triggers feed the same queue.
    s(key="sqs", category="integration", shape="sqs", label="SQS",
      sub="chat + scheduled + DLQ", group_label="Queueing",
      x=C["invoke_a"], y=ROW[0])
    s(key="scheduler", category="integration", shape="eventbridge_scheduler",
      label="EventBridge Scheduler", sub="A1 A2 A4 A5 cadences",
      group_label="Cadences", x=C["invoke_a"], y=ROW[1])
    s(key="invoker", category="compute", shape="lambda", label="Lambda", sub="invoker",
      group_label="Invocation", x=C["invoke_b"], y=ROW[0])
    s(key="eventbus", category="integration", shape="eventbridge", label="EventBridge",
      sub="rules on the existing bus", group_label="Reactive",
      x=C["invoke_b"], y=ROW[1])

    # Zone 3 -- the agent plane, one vertical stack.
    s(key="runtime", category="aiml", shape="bedrock_agentcore",
      label="AgentCore Runtime", sub="orchestrator + A1-A5", group_label="Reasoning",
      x=C["agents"], y=ROW[0])
    s(key="memory", category="aiml", shape="bedrock_agentcore", label="AgentCore Memory",
      sub="facts + shift summaries", group_label="Recall",
      x=C["agents"], y=ROW[1])
    s(key="interpreter", category="aiml", shape="bedrock_agentcore",
      label="Code Interpreter", sub="A5 arithmetic, sandboxed", group_label="Analysis",
      x=C["agents"], y=ROW[2])

    # Zone 4 -- the tool plane. The two interceptors are the guardrail.
    s(key="gateway", category="aiml", shape="bedrock_agentcore",
      label="AgentCore Gateway", sub="29 MCP tools", group_label="Gateway",
      x=C["toolplane"], y=ROW[0])
    s(key="approval", category="compute", shape="lambda", label="Lambda",
      sub="Tier-2 approval gate", group_label="Request interceptor",
      x=C["toolplane"], y=ROW[1])
    s(key="decisionlog", category="compute", shape="lambda", label="Lambda",
      sub="decision log", group_label="Response interceptor",
      x=C["toolplane"], y=ROW[2])

    # Zone 5 -- the only code that touches the platform.
    s(key="tools", category="compute", shape="lambda", label="5 Lambda targets",
      sub="one per agent domain", group_label="Tool targets",
      x=C["targets"], y=ROW[0])
    s(key="secrets", category="security", shape="secrets_manager",
      label="Secrets Manager", sub="one secret per agent", group_label="Credentials",
      x=C["targets"], y=ROW[1])

    # Zone 6 -- external. Nothing here is created or changed by this project.
    s(key="platform-api", category="network", shape="api_gateway",
      label="CRS + PMS APIs", sub="Cognito-authorized", group_label="Platform APIs",
      x=C["platform"], y=ROW[0])
    s(key="platform-db", category="database", shape="aurora", label="Aurora",
      sub="never reached directly", group_label="Platform data",
      x=C["platform"], y=ROW[1])

    # Bands -- what the system writes down.
    s(key="logs", category="integration", shape="cloudwatch_2", label="CloudWatch Logs",
      sub="GenAI spans", group_label="Telemetry", x=B["logs"], y=BAND_ROW)
    s(key="evaluations", category="aiml", shape="bedrock_agentcore",
      label="AgentCore Evaluations", sub="4 built-in + 2 judges", group_label="Grading",
      x=B["evals"], y=BAND_ROW)
    s(key="approvals-table", category="database", shape="dynamodb", label="DynamoDB",
      sub="approvals, console-issued", group_label="Approval tokens",
      x=B["approvals"], y=BAND_ROW)
    s(key="decisions-table", category="database", shape="dynamodb", label="DynamoDB",
      sub="decisions, RETAIN", group_label="Audit trail",
      x=B["decisions"], y=BAND_ROW)

    # ---- edges -------------------------------------------------------------- #
    # The spine: one straight left-to-right line through row 0, zone by zone. If you
    # read nothing else on this diagram, read this.
    e = lambda *a, **kw: edge(root, *a, **kw)  # noqa: E731
    ay = CLOUD["y"]

    e("e-users-cf", "users-container", "cloudfront", label="HTTPS")
    e("e-cf-api", "cloudfront", "apigw", label="/api/*", exit_xy=(1, 0.5),
      entry_xy=(0, 0.5))
    e("e-api-sqs", "apigw", "sqs", label="queue a run", exit_xy=(1, 0.5),
      entry_xy=(0, 0.5))
    e("e-sqs-invoker", "sqs", "invoker", exit_xy=(1, 0.5), entry_xy=(0, 0.5))
    e("e-invoker-runtime", "invoker", "runtime", label="InvokeAgentRuntime",
      exit_xy=(1, 0.5), entry_xy=(0, 0.5), label_y=-14)
    e("e-runtime-gateway", "runtime", "gateway", label="MCP over SigV4",
      exit_xy=(1, 0.5), entry_xy=(0, 0.5), label_y=-14)
    e("e-gateway-tools", "gateway", "tools", exit_xy=(1, 0.5), entry_xy=(0, 0.5))
    e("e-tools-platform", "tools", "platform-api", label="ID token, paced",
      exit_xy=(1, 0.5), entry_xy=(0, 0.5), label_y=-14)

    # What each stage depends on, hanging straight down from it.
    e("e-cf-s3", "cloudfront", "s3", exit_xy=(0.5, 1), entry_xy=(0.5, 0))
    e("e-cognito-api", "cognito", "apigw", label="ID token", exit_xy=(0.5, 0),
      entry_xy=(0.5, 1))
    e("e-runtime-memory", "runtime", "memory", exit_xy=(0.5, 1), entry_xy=(0.5, 0))
    e("e-memory-interp", "memory", "interpreter", exit_xy=(0.5, 1), entry_xy=(0.5, 0))
    e("e-gateway-approval", "gateway", "approval", exit_xy=(0.5, 1), entry_xy=(0.5, 0))
    e("e-approval-log", "approval", "decisionlog", exit_xy=(0.5, 1), entry_xy=(0.5, 0))
    e("e-tools-secrets", "tools", "secrets", exit_xy=(0.5, 1), entry_xy=(0.5, 0))
    e("e-platform", "platform-api", "platform-db", exit_xy=(0.5, 1), entry_xy=(0.5, 0),
      dashed=True)

    # Both unattended triggers enter the queue from below, on separate faces so the
    # two arrows never share a segment.
    e("e-sched-sqs", "scheduler", "sqs", exit_xy=(0.5, 0), entry_xy=(0.25, 1))
    e("e-bus-sqs", "eventbus", "sqs", exit_xy=(0.25, 0), entry_xy=(0.75, 1),
      waypoints=[(CLOUD["x"] + C["invoke_b"] + 30, ay + ROW[0] + 160),
                 (CLOUD["x"] + C["invoke_a"] + 90, ay + ROW[0] + 160)])

    # Down into the bands. Two short drops per zone, using nested lanes.
    e("e-interp-logs", "interpreter", "logs", exit_xy=(0.75, 1), entry_xy=(0.25, 0))
    e("e-logs-eval", "logs", "evaluations", exit_xy=(1, 0.5), entry_xy=(0, 0.5))
    gutter_45 = CLOUD["x"] + (Z["toolplane"][0] + Z["toolplane"][1] + Z["targets"][0]) // 2
    e("e-approval-table", "approval", "approvals-table", exit_xy=(1, 0.5),
      entry_xy=(0.5, 0),
      waypoints=[(gutter_45 - 12, ay + ROW[1] + 60), (gutter_45 - 12, ay + LANE_A),
                 (CLOUD["x"] + B["approvals"] + 60, ay + LANE_A)])
    e("e-log-table", "decisionlog", "decisions-table", exit_xy=(1, 0.5),
      entry_xy=(0.5, 0),
      waypoints=[(gutter_45 + 12, ay + ROW[2] + 60), (gutter_45 + 12, ay + LANE_B),
                 (CLOUD["x"] + B["decisions"] + 60, ay + LANE_B)])

    return mxfile


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    xml = serialise(build())
    # ElementTree escapes the ampersands in our deliberate HTML entities; put them back.
    for bad, good in (("&amp;lt;", "&lt;"), ("&amp;gt;", "&gt;"),
                      ("&amp;quot;", "&quot;"), ("&amp;#", "&#"),
                      ("&amp;nbsp;", "&nbsp;"), ("&amp;amp;", "&amp;")):
        xml = xml.replace(bad, good)
    OUT.write_text(f'<?xml version="1.0" encoding="UTF-8"?>\n{xml}\n')
    print(f"wrote {OUT.relative_to(REPO_ROOT)}  ({OUT.stat().st_size:,} bytes)")
    print(f"page {PAGE_W}x{PAGE_H}")


if __name__ == "__main__":
    main()
