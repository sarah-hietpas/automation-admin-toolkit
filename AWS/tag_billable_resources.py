#!/usr/bin/env python3
"""
tag_billable_resources.py

Interactively tag the billable resources in an AWS account.

The script scans the account, then shows each billable resource one at a time,
with context about what it's attached to, and asks whether it should get your
tags. Nothing changes until the end, when you review the full list and type
"yes". Run it in AWS CloudShell, where Python 3 and boto3 are already
installed, or on your own computer (for example in VS Code's terminal) with
Python 3, boto3 and the AWS CLI. Locally, sign in first with
"aws sso login --profile NAME" and pass the same --profile NAME to the script.

Tags
  Choose the tags with --tag KEY=VALUE, repeated once per tag. Quote values
  that contain spaces. Without --tag, the DEFAULT_TAGS set near the top of this
  file are used. The Name tag is never applied in bulk because it should be
  unique per resource; add --ask-name to be prompted for one instead.

Resources covered
  EC2 (instances, EBS volumes and snapshots, AMIs, Elastic IPs, NAT gateways,
  VPC endpoints), load balancers, RDS, Lambda, DynamoDB, ECS, EKS, ElastiCache,
  EFS, Secrets Manager, KMS customer managed keys, SQS, SNS, EventBridge event
  buses, SES, End User Messaging SMS, MediaConvert, IVS (channels, stages,
  chat rooms), API Gateway stages, Cognito user pools, ECR, SSM advanced
  parameters, OpenSearch (domains, serverless collections), Redshift (clusters,
  serverless), EMR (clusters, serverless), Data Pipeline, Transfer Family,
  QuickSight users, Storage Gateway, CloudWatch log groups and alarms, X-Ray
  groups, WAF web ACLs and rule groups, Security Lake, GuardDuty, Security Hub,
  Config, CloudTrail and S3, plus CloudFront, Route 53 and CloudFront WAF web
  ACLs when us-east-1 is scanned. CloudSearch domains have no tagging API, so
  they're listed at the end for you to tag in the CloudSearch console.
  --billed-services shows whether anything you pay for falls outside this list.

Examples
  python3 tag_billable_resources.py --dry-run                # current region, change nothing
  python3 tag_billable_resources.py --all-regions --tag Environment=Production --tag "Application=Example App"
  python3 tag_billable_resources.py --regions us-east-1 us-west-2 --match exampleapp
  python3 tag_billable_resources.py --all-regions --exclude test dev   # leave out test/dev resources
  python3 tag_billable_resources.py --all-regions --only qa dev test --tag Environment=Dev   # just those
  python3 tag_billable_resources.py --all-regions --billed-services

Choosing resources by name
  --only WORD... keeps only resources whose name, ID, tags or details contain
  one of the words, plus anything attached to them (e.g. the unnamed volume on
  a "qa" instance). --exclude WORD... does the opposite and leaves them out.
  Both ignore case and match parts of words, so "dev" also catches "device";
  use both together to trim those (--only dev --exclude device). Excluded
  resources are listed before the review starts, and every decision is
  recorded in the decision log so you can check them.

Tagging everything that's left
  --skip-tagged KEY=VALUE leaves out resources that already carry that tag, and
  --select-all says yes to everything else without prompting. Together they tag
  the rest of an account in one pass, for example everything not already
  Production as Dev:
    python3 tag_billable_resources.py --all-regions --skip-tagged Environment=Production \\
        --tag Environment=Dev --tag Platform=platformName --select-all --dry-run
  You still review the full list and type "yes" before anything changes. Two
  kinds of resources still stop to ask: ones with a different value already set
  (e.g. Environment=Staging), and ones attached to a resource that was left out
  (e.g. a volume or log group belonging to a Production instance).

Fixing resources tagged with the wrong value
  --only-tagged KEY=VALUE keeps only resources that currently have that tag, and
  --overwrite lets --select-all replace the old value without stopping. To find
  everything tagged Production whose name or tags say qa, dev or test, and
  switch it to Dev:
    python3 tag_billable_resources.py --all-regions --only-tagged Environment=Production \\
        --only qa dev test --tag Environment=Dev --select-all --overwrite --dry-run
  The final list shows each change (Environment Production -> Dev) and where the
  word was found, so false matches like 'dev' in 'devops' are easy to spot and
  leave out with --exclude.

  To give each word its own value instead, use --by-word WORD=VALUE. Each
  resource (and what's attached to it) gets the value for the word it contains;
  ones containing two of the words are listed for you to set by hand:
    python3 tag_billable_resources.py --all-regions --by-word qa=qa dev=dev test=test \\
        --exclude devops device --select-all --overwrite --dry-run
  Only the Environment tag changes (use --by-word-key for another key); add
  --tag to set other tags at the same time.

Suggested answers
  Each prompt suggests Yes when the resource is linked to something that already
  has all of your tags (a volume on a tagged instance, its snapshots, its Elastic
  IP, a load balancer routing to it, a log group or alarm for it), or when its
  name, ID, tags or details contain a --match word. Otherwise it suggests No.
  Tagging a resource during the review makes its related resources linked too.

While reviewing
  Enter   take the suggested answer
  y / n   tag / don't tag this one
  t       type your own tags for just this one (KEY=VALUE, comma-separated);
          they're added to the planned tags, or replace the same key
  b       go back to the previous prompt and answer it again
  a       yes to every remaining resource of this type (it still stops to ask
          before overwriting a different value already on a resource)
  s       no to every remaining resource of this type
  q       stop reviewing and go to the summary (you can still apply or cancel)
  Ctrl+C  quit without changing anything

Resources that already have all of the tags are skipped, so you can stop and
re-run any time. A CSV of every decision is saved to your home folder.
"""

import argparse
import csv
import datetime as dt
import json
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError
except ImportError:
    sys.exit("boto3 isn't installed here. Run:  pip3 install --user boto3")

# Tags applied when no --tag option is given. Edit these to change the defaults.
DEFAULT_TAGS = {"Environment": "environmentTag", "Platform": "platformTag"}
CFG = Config(retries={"max_attempts": 10, "mode": "adaptive"})
INSTANCE_ID_RE = re.compile(r"\bi-[0-9a-f]{8,17}\b")

# Resource types, in review order. Instances come first so that once you tag an
# instance, its volumes, snapshots, IPs and load balancers show up as linked.
INSTANCE = "EC2 instance"
VOLUME = "EBS volume"
SNAPSHOT = "EBS snapshot"
EIP = "Elastic IP"
LB = "Load balancer"
CLB = "Classic load balancer"
NAT = "NAT gateway"
VPCE = "VPC endpoint"
AMI = "AMI"
RDS_CLUSTER = "RDS cluster"
RDS_INSTANCE = "RDS instance"
LAMBDA = "Lambda function"
DDB = "DynamoDB table"
ECS_CLUSTER = "ECS cluster"
ECS_SERVICE = "ECS service"
EKS = "EKS cluster"
CACHE = "ElastiCache cluster"
EFS = "EFS file system"
SECRET = "Secrets Manager secret"
WAF_ACL = "WAF web ACL"
WAF_RULE_GROUP = "WAF rule group"
SECLAKE = "Security Lake data lake"
SECLAKE_SUB = "Security Lake subscriber"
CLOUDSEARCH = "CloudSearch domain"   # listed for tagging by hand; there's no tagging API
OS_DOMAIN = "OpenSearch domain"
OS_COLLECTION = "OpenSearch Serverless collection"
RS_CLUSTER = "Redshift cluster"
RS_WORKGROUP = "Redshift Serverless workgroup"
RS_NAMESPACE = "Redshift Serverless namespace"
EMR_CLUSTER = "EMR cluster"
EMR_APP = "EMR Serverless application"
PIPELINE = "Data Pipeline pipeline"
TRANSFER_SERVER = "Transfer Family server"
TRANSFER_WEBAPP = "Transfer Family web app"
QS_USER = "QuickSight user"
XRAY_GROUP = "X-Ray group"
APIGW_STAGE = "API Gateway stage"
COGNITO_POOL = "Cognito user pool"
ECR_REPO = "ECR repository"
SSM_PARAM = "SSM parameter"
EVENT_BUS = "EventBridge event bus"
IVS_CHANNEL = "IVS channel"
IVS_STAGE = "IVS stage"
IVS_ROOM = "IVS chat room"
SECURITY_HUB = "Security Hub"
CONFIG_RULE = "Config rule"
CONFIG_RECORDER = "Config recorder"
TRAIL = "CloudTrail trail"
TRAIL_STORE = "CloudTrail Lake event data store"
KMS_KEY = "KMS key"
SQS_QUEUE = "SQS queue"
SNS_TOPIC = "SNS topic"
SES_IDENTITY = "SES identity"
SES_CONFIG = "SES configuration set"
SMS_NUMBER = "SMS phone number"
SMS_SENDER = "SMS sender ID"
SMS_POOL = "SMS pool"
SMS_CONFIG = "SMS configuration set"
MC_QUEUE = "MediaConvert queue"
MC_TEMPLATE = "MediaConvert job template"
MC_PRESET = "MediaConvert preset"
SGW = "Storage Gateway"
SGW_VOLUME = "Storage Gateway volume"
SGW_SHARE = "Storage Gateway file share"
S3 = "S3 bucket"
LOG_GROUP = "CloudWatch log group"
ALARM = "CloudWatch alarm"
GUARDDUTY = "GuardDuty detector"
CLOUDFRONT = "CloudFront distribution"
R53_ZONE = "Route 53 hosted zone"
R53_CHECK = "Route 53 health check"
# Log groups and alarms come after the resources they belong to, so a log group
# for a production database or an alarm on a production instance shows as linked.
TYPE_ORDER = [INSTANCE, VOLUME, SNAPSHOT, EIP, LB, CLB, NAT, VPCE, AMI,
              RDS_CLUSTER, RDS_INSTANCE, LAMBDA, APIGW_STAGE, COGNITO_POOL, DDB,
              ECS_CLUSTER, ECS_SERVICE, ECR_REPO, EKS, CACHE, EFS, OS_DOMAIN, OS_COLLECTION,
              RS_CLUSTER, RS_WORKGROUP, RS_NAMESPACE, EMR_CLUSTER, EMR_APP, PIPELINE,
              TRANSFER_SERVER, TRANSFER_WEBAPP, QS_USER, SECRET, SSM_PARAM,
              KMS_KEY, SQS_QUEUE, SNS_TOPIC, EVENT_BUS, SES_IDENTITY, SES_CONFIG,
              SMS_NUMBER, SMS_SENDER, SMS_POOL, SMS_CONFIG, MC_QUEUE, MC_TEMPLATE,
              MC_PRESET, IVS_CHANNEL, IVS_STAGE, IVS_ROOM, SGW, SGW_VOLUME, SGW_SHARE, S3,
              LOG_GROUP, ALARM, XRAY_GROUP, GUARDDUTY, SECURITY_HUB,
              SECLAKE, SECLAKE_SUB, CLOUDSEARCH, CONFIG_RECORDER, CONFIG_RULE,
              TRAIL, TRAIL_STORE, CLOUDFRONT, R53_ZONE, R53_CHECK,
              WAF_ACL, WAF_RULE_GROUP]  # after what they protect (load balancers, APIs, distributions)

# Bill lines that no resource tag can claim (checked before COVERAGE).
# Services whose resources can carry tags, but only through their own console.
MANUAL_ONLY = {
    "cloudsearch": "domains listed at the end to tag in the CloudSearch console; it has no tagging API",
}

NOT_TAGGABLE = {
    "tax": "",
    "security bundle": "monthly commitment fee; can't be tagged",
    "savings plans": "commitment; can't be tagged",
    "support": "support plan; can't be tagged",
}

# Lower-cased pieces of Cost Explorer service names this script scans, with a
# note where only part of that service's bill can carry tags.
COVERAGE = {
    # listed first so they win over the shorter "cloudwatch" entry below
    "cloudwatch events": "event buses", "eventbridge": "event buses",
    "aws waf": "web ACLs + custom rule groups",
    "security lake": "data lake + subscribers; charges may not follow the tag",
    "opensearch": "domains + serverless collections; collection charges may not follow the tag",
    "redshift": "clusters + serverless workgroups and namespaces",
    "transfer family": "servers + web apps",
    "x-ray": "groups; charges may not follow the tag",
    "elastic mapreduce": "clusters + serverless applications; cluster tags also reach its EC2 nodes",
    "data pipeline": "pipelines",
    "quicksight": "users; capacity and SPICE charges can't be tagged",
    "interactive video service": "channels, real-time stages, chat rooms",
    "api gateway": "API stages, which is where API Gateway costs are tracked",
    "cognito": "user pools",
    "security hub": "hub; charges may not follow the tag",
    "aws config": "rules + recorder; charges may not follow the tag",
    "cloudtrail": "trails + Lake event data stores; trail tags don't reach billing",
    "container registry": "repositories",
    "systems manager": "advanced parameters",
    "mediaconvert": "queues, job templates, custom presets; see Billing tag source",
    "end user messaging": "phone numbers, sender IDs, pools, configuration sets",
    "simple email service": "identities + configuration sets; charges may not follow the tag",
    "key management service": "customer managed keys",
    "simple queue service": "", "simple notification service": "",
    "elastic compute cloud": "", "ec2 - other": "", "relational database": "",
    "simple storage service": "", "lambda": "", "dynamodb": "", "load balancing": "",
    "container service": "", "elasticache": "", "elastic file system": "",
    "secrets manager": "", "cloudfront": "", "virtual private cloud": "",
    "route 53": "hosted zones + health checks",
    "cloudwatch": "log groups + alarms; metrics and dashboards can't be tagged",
    "guardduty": "detectors; charges may not follow the tag",
    "storage gateway": "gateways, volumes, file shares",
}

# Log group names that point at a specific resource.
LOG_GROUP_PATTERNS = [re.compile(p) for p in (
    r"^/aws/lambda/([^/]+)", r"^/aws/rds/(?:instance|cluster)/([^/]+)/",
    r"^/aws/eks/([^/]+)/cluster", r"^/aws/elasticache/([^/]+)")]

# ---------------------------------------------------------------- output helpers
USE_COLOR = sys.stdout.isatty()


def _c(code, s):
    return f"\033[{code}m{s}\033[0m" if USE_COLOR else s


def bold(s): return _c("1", s)
def green(s): return _c("32", s)
def yellow(s): return _c("33", s)
def dim(s): return _c("2", s)


def fmt_tags(tags, limit=110):
    s = ", ".join(f"{k}={v}" for k, v in tags.items())
    return s if len(s) <= limit else s[:limit - 3] + "..."


# ---------------------------------------------------------------- data model
@dataclass(eq=False)
class Res:
    region: str
    rtype: str
    rid: str
    arn: str
    tags: dict = field(default_factory=dict)
    name: str = ""
    detail: str = ""
    via: str = "arn"         # "ec2" -> ec2.create_tags by ID; "arn" -> Resource Groups Tagging API
    api_region: str = ""     # endpoint used when tagging (defaults to region)
    relations: list = field(default_factory=list)  # (kind, ref, verb); kind = instance | volume | vpc | res
    extra: dict = field(default_factory=dict)      # service-specific details needed to tag it

    def __post_init__(self):
        self.api_region = self.api_region or self.region
        self.name = self.name or self.tags.get("Name", "")


@dataclass
class Ctx:
    session: object
    region: str
    acct: str
    part: str
    tag_index: dict

    def client(self, svc):
        return self.session.client(svc, region_name=self.region, config=CFG)

    def arn(self, svc, resource, with_account=True):
        return f"arn:{self.part}:{svc}:{self.region}:{self.acct if with_account else ''}:{resource}"


def tags_to_dict(tag_list):
    return {t["Key"]: t.get("Value", "") for t in tag_list or []}


def has_all(tags, target):
    return all(tags.get(k) == v for k, v in target.items())


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def paged(client, op, key, **kw):
    if client.can_paginate(op):
        for page in client.get_paginator(op).paginate(**kw):
            yield from page.get(key) or []
        return
    call = getattr(client, op)  # no paginator in this SDK: follow the next-page token by hand
    while True:
        resp = call(**kw)
        yield from resp.get(key) or []
        token = next((k for k in ("NextToken", "nextToken") if resp.get(k)), None)
        if not token:
            return
        kw[token] = resp[token]


def token_paged(call, key, **kw):
    """Follow NextToken by hand, for operations boto3 has no paginator for."""
    while True:
        resp = call(**kw)
        yield from resp.get(key) or []
        if not resp.get("NextToken"):
            return
        kw["NextToken"] = resp["NextToken"]


def err_text(e):
    if isinstance(e, ClientError):
        return e.response.get("Error", {}).get("Message") or str(e)
    return str(e).splitlines()[0]


def attempt(what, where, fn, *args, default=None):
    """Run a collector; on an AWS error print a warning and keep going."""
    try:
        return fn(*args)
    except EndpointConnectionError:  # the service isn't offered in this region
        return [] if default is None else default
    except (ClientError, BotoCoreError) as e:
        msg = str(e).splitlines()[0][:160]
        print(yellow(f"   ! {where}: couldn't read {what} - {msg}"))
        return [] if default is None else default


# ---------------------------------------------------------------- collectors
def build_tag_index(session, region):
    """ARN -> tags for everything in the region that has tags (one paged call)."""
    tg = session.client("resourcegroupstaggingapi", region_name=region, config=CFG)
    return {m["ResourceARN"]: tags_to_dict(m.get("Tags"))
            for m in paged(tg, "get_resources", "ResourceTagMappingList")}


def ec2_instances(c):
    out = []
    states = ["pending", "running", "stopping", "stopped"]
    for resv in paged(c.client("ec2"), "describe_instances", "Reservations",
                      Filters=[{"Name": "instance-state-name", "Values": states}]):
        for i in resv["Instances"]:
            r = Res(c.region, INSTANCE, i["InstanceId"], c.arn("ec2", f"instance/{i['InstanceId']}"),
                    tags=tags_to_dict(i.get("Tags")), via="ec2",
                    detail=f"{i['InstanceType']}, {i['State']['Name']}")
            if i.get("VpcId"):
                r.relations.append(("vpc", i["VpcId"], "in"))
            if r.tags.get("aws:elasticmapreduce:job-flow-id"):  # EMR copies cluster tags to its nodes
                r.relations.append(("res", r.tags["aws:elasticmapreduce:job-flow-id"], "node of EMR cluster"))
            out.append(r)
    return out


def ec2_volumes(c):
    out = []
    for v in paged(c.client("ec2"), "describe_volumes", "Volumes"):
        r = Res(c.region, VOLUME, v["VolumeId"], c.arn("ec2", f"volume/{v['VolumeId']}"),
                tags=tags_to_dict(v.get("Tags")), via="ec2",
                detail=f"{v['Size']} GiB {v['VolumeType']}, {v['State']}")
        for a in v.get("Attachments") or []:
            r.relations.append(("instance", a["InstanceId"], "attached to"))
        if not v.get("Attachments"):
            r.detail += " (unattached, still billed)"
        out.append(r)
    return out


def ec2_snapshots(c):
    out = []
    for s in paged(c.client("ec2"), "describe_snapshots", "Snapshots", OwnerIds=["self"]):
        r = Res(c.region, SNAPSHOT, s["SnapshotId"],
                c.arn("ec2", f"snapshot/{s['SnapshotId']}", with_account=False),
                tags=tags_to_dict(s.get("Tags")), via="ec2",
                detail=f"{s.get('VolumeSize', '?')} GiB, taken {s['StartTime']:%Y-%m-%d}")
        vol = s.get("VolumeId")
        if vol and vol != "vol-ffffffff":
            r.relations.append(("volume", vol, "snapshot of"))
        for iid in sorted(set(INSTANCE_ID_RE.findall(s.get("Description") or ""))):
            r.relations.append(("instance", iid, "created from"))
        out.append(r)
    return out


def ec2_addresses(c):
    out = []
    for a in c.client("ec2").describe_addresses().get("Addresses", []):
        alloc = a.get("AllocationId")
        if not alloc:
            continue
        r = Res(c.region, EIP, alloc, c.arn("ec2", f"elastic-ip/{alloc}"),
                tags=tags_to_dict(a.get("Tags")), via="ec2", detail=a.get("PublicIp", ""))
        if a.get("InstanceId"):
            r.relations.append(("instance", a["InstanceId"], "associated with"))
        else:
            r.detail += " (not associated, still billed)"
        out.append(r)
    return out


def ec2_nat_gateways(c):
    out = []
    for n in paged(c.client("ec2"), "describe_nat_gateways", "NatGateways",
                   Filter=[{"Name": "state", "Values": ["pending", "available"]}]):
        r = Res(c.region, NAT, n["NatGatewayId"], c.arn("ec2", f"natgateway/{n['NatGatewayId']}"),
                tags=tags_to_dict(n.get("Tags")), via="ec2",
                detail=f"{n.get('ConnectivityType', 'public')}, {n['State']}")
        r.relations.append(("vpc", n["VpcId"], "in"))
        out.append(r)
    return out


def ec2_vpc_endpoints(c):
    # Gateway endpoints (S3/DynamoDB) are free, so only the billed kinds are listed.
    out = []
    for e in paged(c.client("ec2"), "describe_vpc_endpoints", "VpcEndpoints",
                   Filters=[{"Name": "vpc-endpoint-type", "Values": ["Interface", "GatewayLoadBalancer"]}]):
        if (e.get("State") or "").lower() in ("deleted", "deleting", "failed", "rejected"):
            continue
        r = Res(c.region, VPCE, e["VpcEndpointId"], c.arn("ec2", f"vpc-endpoint/{e['VpcEndpointId']}"),
                tags=tags_to_dict(e.get("Tags")), via="ec2",
                detail=f"{e.get('VpcEndpointType')} endpoint for {e.get('ServiceName', '?')}")
        r.relations.append(("vpc", e["VpcId"], "in"))
        out.append(r)
    return out


def ec2_images(c):
    out = []
    for img in c.client("ec2").describe_images(Owners=["self"]).get("Images", []):
        tags = tags_to_dict(img.get("Tags"))
        out.append(Res(c.region, AMI, img["ImageId"],
                       c.arn("ec2", f"image/{img['ImageId']}", with_account=False),
                       tags=tags, name=tags.get("Name") or img.get("Name", ""), via="ec2",
                       detail=f"created {(img.get('CreationDate') or '?')[:10]}; billed through its snapshots"))
    return out


def elbv2_load_balancers(c):
    elb = c.client("elbv2")
    out = []
    for lb in paged(elb, "describe_load_balancers", "LoadBalancers"):
        arn = lb["LoadBalancerArn"]
        r = Res(c.region, LB, lb["LoadBalancerName"], arn, tags=c.tag_index.get(arn, {}),
                detail=f"{lb.get('Type', '')}, {lb.get('Scheme', '')}")
        if lb.get("VpcId"):
            r.relations.append(("vpc", lb["VpcId"], "in"))
        try:
            for tgrp in paged(elb, "describe_target_groups", "TargetGroups", LoadBalancerArn=arn):
                if tgrp.get("TargetType") != "instance":
                    continue
                health = elb.describe_target_health(TargetGroupArn=tgrp["TargetGroupArn"])
                for d in health.get("TargetHealthDescriptions", []):
                    r.relations.append(("instance", d["Target"]["Id"], "routes to"))
        except ClientError:
            pass  # targets are context only
        out.append(r)
    return out


def elb_classic(c):
    out = []
    for lb in paged(c.client("elb"), "describe_load_balancers", "LoadBalancerDescriptions"):
        name = lb["LoadBalancerName"]
        arn = c.arn("elasticloadbalancing", f"loadbalancer/{name}")
        r = Res(c.region, CLB, name, arn, tags=c.tag_index.get(arn, {}), detail=lb.get("Scheme", ""))
        for inst in lb.get("Instances", []):
            r.relations.append(("instance", inst["InstanceId"], "routes to"))
        if lb.get("VPCId"):
            r.relations.append(("vpc", lb["VPCId"], "in"))
        out.append(r)
    return out


def rds_clusters(c):
    return [Res(c.region, RDS_CLUSTER, cl["DBClusterIdentifier"], cl["DBClusterArn"],
                tags=tags_to_dict(cl.get("TagList")),
                detail=f"{cl.get('Engine', '')}, {cl.get('Status', '')}")
            for cl in paged(c.client("rds"), "describe_db_clusters", "DBClusters")]


def rds_instances(c):
    out = []
    for db in paged(c.client("rds"), "describe_db_instances", "DBInstances"):
        r = Res(c.region, RDS_INSTANCE, db["DBInstanceIdentifier"], db["DBInstanceArn"],
                tags=tags_to_dict(db.get("TagList")),
                detail=f"{db.get('DBInstanceClass', '')} {db.get('Engine', '')}, {db.get('DBInstanceStatus', '')}")
        if db.get("DBClusterIdentifier"):
            r.detail += f", member of {db['DBClusterIdentifier']}"
        vpc = (db.get("DBSubnetGroup") or {}).get("VpcId")
        if vpc:
            r.relations.append(("vpc", vpc, "in"))
        out.append(r)
    return out


def lambda_functions(c):
    out = []
    for f in paged(c.client("lambda"), "list_functions", "Functions"):
        arn = f["FunctionArn"]
        r = Res(c.region, LAMBDA, f["FunctionName"], arn, tags=c.tag_index.get(arn, {}),
                detail=f"{f.get('Runtime', 'container image')}, {f.get('MemorySize', '?')} MB")
        vpc = (f.get("VpcConfig") or {}).get("VpcId")
        if vpc:
            r.relations.append(("vpc", vpc, "in"))
        out.append(r)
    return out


def dynamodb_tables(c):
    out = []
    for name in paged(c.client("dynamodb"), "list_tables", "TableNames"):
        arn = c.arn("dynamodb", f"table/{name}")
        out.append(Res(c.region, DDB, name, arn, tags=c.tag_index.get(arn, {})))
    return out


def ecs_resources(c):
    ecs = c.client("ecs")
    out = []
    for carn in paged(ecs, "list_clusters", "clusterArns"):
        cname = carn.split("/")[-1]
        out.append(Res(c.region, ECS_CLUSTER, cname, carn, tags=c.tag_index.get(carn, {})))
        for sarn in paged(ecs, "list_services", "serviceArns", cluster=carn):
            out.append(Res(c.region, ECS_SERVICE, sarn.split("/")[-1], sarn,
                           tags=c.tag_index.get(sarn, {}), detail=f"in cluster {cname}"))
    return out


def eks_clusters(c):
    eks = c.client("eks")
    out = []
    for name in paged(eks, "list_clusters", "clusters"):
        cl = eks.describe_cluster(name=name)["cluster"]
        r = Res(c.region, EKS, name, cl["arn"], tags=cl.get("tags") or {},
                detail=f"Kubernetes {cl.get('version', '?')}")
        vpc = (cl.get("resourcesVpcConfig") or {}).get("vpcId")
        if vpc:
            r.relations.append(("vpc", vpc, "in"))
        out.append(r)
    return out


def elasticache_clusters(c):
    return [Res(c.region, CACHE, cc["CacheClusterId"], cc["ARN"], tags=c.tag_index.get(cc["ARN"], {}),
                detail=f"{cc.get('Engine', '')} {cc.get('CacheNodeType', '')}")
            for cc in paged(c.client("elasticache"), "describe_cache_clusters", "CacheClusters")]


def efs_file_systems(c):
    out = []
    for fs in paged(c.client("efs"), "describe_file_systems", "FileSystems"):
        tags = tags_to_dict(fs.get("Tags"))
        arn = fs.get("FileSystemArn") or c.arn("elasticfilesystem", f"file-system/{fs['FileSystemId']}")
        size = (fs.get("SizeInBytes") or {}).get("Value", 0) / 1024 ** 3
        out.append(Res(c.region, EFS, fs["FileSystemId"], arn, tags=tags,
                       name=tags.get("Name") or fs.get("Name", ""), detail=f"{size:.1f} GiB"))
    return out


def secrets(c):
    out, managed = [], Counter()
    for s in paged(c.client("secretsmanager"), "list_secrets", "SecretList"):
        # Secrets created and owned by another service (e.g. EventBridge connections, named
        # "events!connection/...") can only be changed by that service, so they're skipped.
        owner = s.get("OwningService") or (s["Name"].split("!", 1)[0] if "!" in s["Name"] else "")
        if owner:
            managed[owner] += 1
            continue
        out.append(Res(c.region, SECRET, s["Name"], s["ARN"], tags=tags_to_dict(s.get("Tags"))))
    if managed:
        owners = ", ".join(f"{n} owned by {o}" for o, n in managed.items())
        print(dim(f"   {c.region}: skipped secrets managed by other AWS services ({owners}); they can't be tagged"))
    return out


def _waf_list(waf, op, key, **kw):
    """WAF pages with NextMarker and has no boto3 paginators."""
    marker = None
    while True:
        resp = getattr(waf, op)(Limit=100, **kw, **({"NextMarker": marker} if marker else {}))
        items = resp.get(key) or []
        yield from items
        marker = resp.get("NextMarker")
        if not marker or not items:
            return


def _waf_tags(waf, arn):
    try:
        info = waf.list_tags_for_resource(ResourceARN=arn).get("TagInfoForResource") or {}
        return tags_to_dict(info.get("TagList"))
    except ClientError:
        return {}


def _waf_scope(waf, scope, region, api_region, cloudfront=None):
    out = []
    for acl in _waf_list(waf, "list_web_acls", "WebACLs", Scope=scope):
        arn = acl["ARN"]
        r = Res(region, WAF_ACL, acl["Name"], arn, tags=_waf_tags(waf, arn), via="wafv2", api_region=api_region,
                detail="web ACL for CloudFront" if scope == "CLOUDFRONT" else "regional web ACL")
        try:  # what it protects, so a web ACL in front of production shows as linked
            if cloudfront is not None:
                dl = cloudfront.list_distributions_by_web_acl_id(WebACLId=arn).get("DistributionList") or {}
                targets = [d["ARN"] for d in dl.get("Items") or []]
            else:
                targets = []
                for rt in ("APPLICATION_LOAD_BALANCER", "API_GATEWAY", "COGNITO_USER_POOL"):
                    targets += waf.list_resources_for_web_acl(WebACLArn=arn, ResourceType=rt).get("ResourceArns") or []
            for t in targets:
                r.relations.append(("res", t, "protects"))
            if not targets:
                r.detail += ", not attached to anything (still billed)"
        except (ClientError, BotoCoreError):
            pass
        out.append(r)
    for g in _waf_list(waf, "list_rule_groups", "RuleGroups", Scope=scope):
        out.append(Res(region, WAF_RULE_GROUP, g["Name"], g["ARN"], tags=_waf_tags(waf, g["ARN"]), via="wafv2",
                       api_region=api_region, detail="custom rule group" + (" for CloudFront" if scope == "CLOUDFRONT" else "")))
    return out


def waf_regional(c):
    return _waf_scope(c.client("wafv2"), "REGIONAL", c.region, c.region)


def waf_cloudfront(session):
    # Web ACLs for CloudFront are global and are only reachable through us-east-1.
    return _waf_scope(session.client("wafv2", region_name="us-east-1", config=CFG), "CLOUDFRONT",
                      "global", "us-east-1", cloudfront=session.client("cloudfront", config=CFG))


def security_lake(c):
    sl = c.client("securitylake")
    try:
        lakes = sl.list_data_lakes(regions=[c.region]).get("dataLakes") or []
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("ResourceNotFoundException", "AccessDeniedException"):
            return []  # Security Lake isn't set up in this region, or is managed from another account
        raise

    def tags_for(arn):
        try:
            return {t["key"]: t.get("value", "") for t in sl.list_tags_for_resource(resourceArn=arn).get("tags") or []}
        except ClientError:
            return {}

    out = []
    for lk in lakes:
        arn = lk.get("dataLakeArn")
        if not arn or lk.get("createStatus") == "FAILED":
            continue
        out.append(Res(c.region, SECLAKE, "data-lake", arn, tags=tags_for(arn), name="Security Lake",
                       via="securitylake", detail="data lake settings for this region (charges may not follow this tag)"))
    if out:
        for s in paged(sl, "list_subscribers", "subscribers"):
            arn = s["subscriberArn"]
            out.append(Res(c.region, SECLAKE_SUB, s.get("subscriberName") or s.get("subscriberId", ""), arn,
                           tags=tags_for(arn), via="securitylake",
                           detail=f"subscriber, {(s.get('subscriberStatus') or '').lower()}".rstrip(", ")))
    return out


MANUAL = []  # resources that can only be tagged by hand in their own console


def cloudsearch_domains(c):
    # CloudSearch has no public tagging API, so its domains are listed for tagging by hand.
    try:
        domains = c.client("cloudsearch").describe_domains().get("DomainStatusList") or []
    except ClientError as e:
        # CloudSearch is closed to new customers: accounts that never used it get NotAuthorized
        # ("New domain creation not supported on this account"), which just means no domains.
        if e.response.get("Error", {}).get("Code") == "NotAuthorized":
            return []
        raise
    for d in domains:
        if d.get("Deleted"):
            continue
        arn = d.get("ARN") or c.arn("cloudsearch", f"domain/{d['DomainName']}")
        n = d.get("SearchInstanceCount")
        MANUAL.append(Res(c.region, CLOUDSEARCH, d["DomainName"], arn, tags=c.tag_index.get(arn, {}), via="manual",
                          detail=f"{n} x {d.get('SearchInstanceType', '')}" if n else "search domain"))
    return []


def opensearch_domains(c):
    oss = c.client("opensearch")
    names = [d["DomainName"] for d in oss.list_domain_names().get("DomainNames") or []]
    out = []
    for batch in chunks(names, 5):  # API limit: 5 domains per describe call
        for d in oss.describe_domains(DomainNames=batch).get("DomainStatusList") or []:
            if d.get("Deleted"):
                continue
            arn = d["ARN"]
            try:
                tags = tags_to_dict(oss.list_tags(ARN=arn).get("TagList"))
            except ClientError:
                tags = {}
            cc = d.get("ClusterConfig") or {}
            out.append(Res(c.region, OS_DOMAIN, d["DomainName"], arn, tags=tags, via="opensearch",
                           detail=f"{d.get('EngineVersion', '')}, {cc.get('InstanceCount', '?')} x "
                                  f"{cc.get('InstanceType', '?')}"))
    return out


def opensearch_collections(c):
    aoss = c.client("opensearchserverless")
    out = []
    for col in paged(aoss, "list_collections", "collectionSummaries"):
        arn = col["arn"]
        try:
            tags = {t["key"]: t.get("value", "") for t in aoss.list_tags_for_resource(resourceArn=arn).get("tags") or []}
        except ClientError:
            tags = {}
        out.append(Res(c.region, OS_COLLECTION, col.get("name") or col["id"], arn, tags=tags, via="aoss",
                       detail=f"serverless collection, {(col.get('status') or '').lower()} "
                              "(charges may not follow this tag)"))
    return out


def redshift_clusters(c):
    out = []
    for cl in paged(c.client("redshift"), "describe_clusters", "Clusters"):
        cid = cl["ClusterIdentifier"]
        out.append(Res(c.region, RS_CLUSTER, cid, c.arn("redshift", f"cluster:{cid}"),
                       tags=tags_to_dict(cl.get("Tags")), via="redshift",
                       detail=f"{cl.get('NumberOfNodes', '?')} x {cl.get('NodeType', '')}, {cl.get('ClusterStatus', '')}"))
    return out


def redshift_serverless(c):
    rss = c.client("redshift-serverless")

    def tags_for(arn):
        try:
            return {t["key"]: t.get("value", "") for t in rss.list_tags_for_resource(resourceArn=arn).get("tags") or []}
        except ClientError:
            return {}

    out, ns_workgroups = [], defaultdict(list)
    for wg in paged(rss, "list_workgroups", "workgroups"):
        arn = wg["workgroupArn"]
        ns_workgroups[wg.get("namespaceName", "")].append(wg["workgroupName"])
        out.append(Res(c.region, RS_WORKGROUP, wg["workgroupName"], arn, tags=tags_for(arn), via="redshift-serverless",
                       detail=f"serverless workgroup, base {wg.get('baseCapacity', '?')} RPUs, "
                              f"{(wg.get('status') or '').lower()}"))
    for ns in paged(rss, "list_namespaces", "namespaces"):
        arn = ns["namespaceArn"]
        r = Res(c.region, RS_NAMESPACE, ns["namespaceName"], arn, tags=tags_for(arn), via="redshift-serverless",
                detail="serverless namespace (stored data)")
        for wg_name in ns_workgroups.get(ns["namespaceName"], []):
            r.relations.append(("res", wg_name, "storage for workgroup"))
        out.append(r)
    return out


def emr_clusters(c):
    emr = c.client("emr")
    out = []
    for cl in paged(emr, "list_clusters", "Clusters",
                    ClusterStates=["STARTING", "BOOTSTRAPPING", "RUNNING", "WAITING"]):
        info = emr.describe_cluster(ClusterId=cl["Id"])["Cluster"]
        tags = tags_to_dict(info.get("Tags"))
        state = ((cl.get("Status") or {}).get("State") or "").lower()
        out.append(Res(c.region, EMR_CLUSTER, cl["Id"],
                       cl.get("ClusterArn") or c.arn("elasticmapreduce", f"cluster/{cl['Id']}"),
                       tags=tags, name=tags.get("Name") or cl.get("Name", ""), via="emr",
                       detail=f"cluster, {state}; tags also go to its EC2 nodes"))
    return out


def emr_serverless_apps(c):
    es = c.client("emr-serverless")
    out = []
    for a in paged(es, "list_applications", "applications"):
        if a.get("state") == "TERMINATED":
            continue
        arn = a["arn"]
        try:
            tags = es.list_tags_for_resource(resourceArn=arn).get("tags") or {}
        except ClientError:
            tags = {}
        out.append(Res(c.region, EMR_APP, a.get("name") or a["id"], arn, tags=tags, via="emr-serverless",
                       detail=f"{a.get('type', '')} serverless application, {(a.get('state') or '').lower()}"))
    return out


def data_pipelines(c):
    dp = c.client("datapipeline")
    ids = [p["id"] for p in paged(dp, "list_pipelines", "pipelineIdList")]
    out = []
    for batch in chunks(ids, 25):  # API limit: 25 per describe call
        for p in dp.describe_pipelines(pipelineIds=batch).get("pipelineDescriptionList") or []:
            tags = {t["key"]: t.get("value", "") for t in p.get("tags") or []}
            state = next((f.get("stringValue", "") for f in p.get("fields") or [] if f.get("key") == "@pipelineState"), "")
            out.append(Res(c.region, PIPELINE, p["pipelineId"], c.arn("datapipeline", f"pipeline/{p['pipelineId']}"),
                           tags=tags, name=tags.get("Name") or p.get("name", ""), via="datapipeline",
                           detail=f"pipeline, {state.lower()}".rstrip(", ")))
    return out


def transfer_resources(c):
    tr = c.client("transfer")

    def tags_for(arn):
        try:
            return tags_to_dict(tr.list_tags_for_resource(Arn=arn).get("Tags"))
        except ClientError:
            return {}

    out = []
    for s in paged(tr, "list_servers", "Servers"):
        out.append(Res(c.region, TRANSFER_SERVER, s["ServerId"], s["Arn"], tags=tags_for(s["Arn"]), via="transfer",
                       detail=f"{s.get('Domain', '')} {s.get('EndpointType', '')} server, "
                              f"{(s.get('State') or '').lower()}".strip()))
    if hasattr(tr, "list_web_apps"):  # newer SDKs only
        for w in paged(tr, "list_web_apps", "WebApps"):
            out.append(Res(c.region, TRANSFER_WEBAPP, w["WebAppId"], w["Arn"], tags=tags_for(w["Arn"]),
                           via="transfer", detail="web app"))
    return out


def quicksight_users(c):
    # QuickSight cost allocation works through tags on users; capacity and SPICE charges can't be tagged.
    qs = c.client("quicksight")
    try:
        users = list(paged(qs, "list_users", "UserList", AwsAccountId=c.acct, Namespace="default"))
    except ClientError as e:
        code, msg = e.response.get("Error", {}).get("Code", ""), err_text(e).lower()
        if code in ("ResourceNotFoundException", "UnsupportedUserEditionException") or "identity region" in msg:
            return []  # QuickSight isn't set up, or its users live in another (identity) region
        raise
    out = []
    for u in users:
        arn = u["Arn"]
        try:
            tags = tags_to_dict(qs.list_tags_for_resource(ResourceArn=arn).get("Tags"))
        except ClientError:
            tags = {}
        detail = f"{(u.get('Role') or '').lower()} user, {u.get('Email', '')}".strip(", ")
        if u.get("Active") is False:
            detail += ", inactive"
        out.append(Res(c.region, QS_USER, u.get("UserName", ""), arn, tags=tags, via="quicksight", detail=detail))
    return out


def xray_groups(c):
    xr = c.client("xray")
    out = []
    for g in paged(xr, "get_groups", "Groups"):
        arn = g["GroupARN"]
        try:
            tags = tags_to_dict(xr.list_tags_for_resource(ResourceARN=arn).get("Tags"))
        except ClientError:
            tags = {}
        out.append(Res(c.region, XRAY_GROUP, g["GroupName"], arn, tags=tags, via="xray",
                       detail="trace group (charges may not follow this tag)"))
    return out


def apigateway_rest_stages(c):
    # API Gateway recommends tagging stages for cost allocation; tags on the API itself
    # are only inherited for access control, not billing.
    apigw = c.client("apigateway")
    out = []
    for api in paged(apigw, "get_rest_apis", "items"):
        for s in apigw.get_stages(restApiId=api["id"]).get("item") or []:
            arn = f"arn:{c.part}:apigateway:{c.region}::/restapis/{api['id']}/stages/{s['stageName']}"
            r = Res(c.region, APIGW_STAGE, f"{api['id']}/{s['stageName']}", arn, tags=s.get("tags") or {},
                    name=(s.get("tags") or {}).get("Name") or api.get("name", ""), via="apigw",
                    detail=f"REST API stage '{s['stageName']}'")
            out.append(r)
    return out


def apigateway_v2_stages(c):
    v2 = c.client("apigatewayv2")
    out = []
    for api in paged(v2, "get_apis", "Items"):
        for s in paged(v2, "get_stages", "Items", ApiId=api["ApiId"]):
            arn = f"arn:{c.part}:apigateway:{c.region}::/apis/{api['ApiId']}/stages/{s['StageName']}"
            out.append(Res(c.region, APIGW_STAGE, f"{api['ApiId']}/{s['StageName']}", arn,
                           tags=s.get("Tags") or {}, name=(s.get("Tags") or {}).get("Name") or api.get("Name", ""),
                           via="apigw2", detail=f"{api.get('ProtocolType', '')} API stage '{s['StageName']}'"))
    return out


def cognito_user_pools(c):
    idp = c.client("cognito-idp")
    out = []
    for p in paged(idp, "list_user_pools", "UserPools", MaxResults=60):
        pool = idp.describe_user_pool(UserPoolId=p["Id"])["UserPool"]
        tags = pool.get("UserPoolTags") or {}
        users = pool.get("EstimatedNumberOfUsers")
        out.append(Res(c.region, COGNITO_POOL, p["Id"], pool.get("Arn") or c.arn("cognito-idp", f"userpool/{p['Id']}"),
                       tags=tags, name=tags.get("Name") or p.get("Name", ""), via="cognito",
                       detail=f"user pool, about {users:,} users" if users is not None else "user pool"))
    return out


def ecr_repositories(c):
    ecr = c.client("ecr")
    out = []
    for repo in paged(ecr, "describe_repositories", "repositories"):
        arn = repo["repositoryArn"]
        try:
            tags = tags_to_dict(ecr.list_tags_for_resource(resourceArn=arn).get("tags"))
        except ClientError:
            tags = {}
        out.append(Res(c.region, ECR_REPO, repo["repositoryName"], arn, tags=tags, via="ecr",
                       detail="container image repository"))
    return out


def ssm_parameters(c):
    # Only advanced-tier parameters are billed; standard ones are free.
    ssm = c.client("ssm")
    out = []
    for p in paged(ssm, "describe_parameters", "Parameters",
                   ParameterFilters=[{"Key": "Tier", "Values": ["Advanced"]}]):
        name = p["Name"]
        arn = p.get("ARN") or c.arn("ssm", "parameter" + (name if name.startswith("/") else "/" + name))
        try:
            tags = tags_to_dict(ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId=name).get("TagList"))
        except ClientError:
            tags = {}
        out.append(Res(c.region, SSM_PARAM, name, arn, tags=tags, via="ssm",
                       detail=f"advanced {p.get('Type', '')} parameter".replace("  ", " ")))
    return out


def event_buses(c):
    ev = c.client("events")
    out = []
    for b in paged(ev, "list_event_buses", "EventBuses"):
        arn = b["Arn"]
        try:
            tags = tags_to_dict(ev.list_tags_for_resource(ResourceARN=arn).get("Tags"))
        except ClientError:
            tags = {}
        out.append(Res(c.region, EVENT_BUS, b["Name"], arn, tags=tags, via="events", detail="event bus"))
    return out


def ivs_channels(c):
    out = []
    for ch in paged(c.client("ivs"), "list_channels", "channels"):
        tags = ch.get("tags") or {}
        out.append(Res(c.region, IVS_CHANNEL, ch["arn"].split("/")[-1], ch["arn"], tags=tags,
                       name=tags.get("Name") or ch.get("name", ""), via="ivs",
                       detail=f"{ch.get('type', '')} channel, {ch.get('latencyMode', '')} latency".lower()))
    return out


def ivs_stages(c):
    out = []
    for st in paged(c.client("ivs-realtime"), "list_stages", "stages"):
        tags = st.get("tags") or {}
        out.append(Res(c.region, IVS_STAGE, st["arn"].split("/")[-1], st["arn"], tags=tags,
                       name=tags.get("Name") or st.get("name", ""), via="ivs-realtime",
                       detail="real-time stage" + (", live now" if st.get("activeSessionId") else "")))
    return out


def ivs_chat_rooms(c):
    out = []
    for rm in paged(c.client("ivschat"), "list_rooms", "rooms"):
        tags = rm.get("tags") or {}
        out.append(Res(c.region, IVS_ROOM, rm.get("id") or rm["arn"].split("/")[-1], rm["arn"], tags=tags,
                       name=tags.get("Name") or rm.get("name", ""), via="ivschat", detail="chat room"))
    return out


def security_hub(c):
    sh = c.client("securityhub")
    try:
        arn = sh.describe_hub()["HubArn"]
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("InvalidAccessException", "ResourceNotFoundException"):
            return []  # Security Hub isn't enabled in this region
        raise
    try:
        tags = sh.list_tags_for_resource(ResourceArn=arn).get("Tags") or {}
    except ClientError:
        tags = {}
    return [Res(c.region, SECURITY_HUB, "hub", arn, tags=tags, name="Security Hub", via="securityhub",
                detail="hub (charges may not follow this tag)")]


def config_resources(c):
    cfg = c.client("config")

    def tags_for(arn):
        try:
            return tags_to_dict(cfg.list_tags_for_resource(ResourceArn=arn).get("Tags"))
        except ClientError:
            return {}

    out = []
    for rec in cfg.describe_configuration_recorders().get("ConfigurationRecorders") or []:
        if rec.get("arn"):  # older recorders without an ARN can't be tagged
            out.append(Res(c.region, CONFIG_RECORDER, rec.get("name", ""), rec["arn"], tags=tags_for(rec["arn"]),
                           via="config", detail="configuration recorder (charges may not follow this tag)"))
    for rule in paged(cfg, "describe_config_rules", "ConfigRules"):
        if rule.get("CreatedBy"):  # rules owned by another service (e.g. Security Hub) can't be changed
            continue
        arn = rule["ConfigRuleArn"]
        source = (rule.get("Source") or {}).get("SourceIdentifier", "")
        out.append(Res(c.region, CONFIG_RULE, rule["ConfigRuleName"], arn, tags=tags_for(arn), via="config",
                       detail=f"rule {source}".strip()))
    return out


def cloudtrail_resources(c):
    ct = c.client("cloudtrail")
    items = []
    for t in paged(ct, "list_trails", "Trails"):
        # each trail is listed once, in its home region; organization trails belong to the management account
        if t.get("HomeRegion") == c.region and f":{c.acct}:" in t["TrailARN"]:
            items.append((TRAIL, t["Name"], t["TrailARN"], "trail (trail tags don't reach billing)"))
    try:
        for e in paged(ct, "list_event_data_stores", "EventDataStores"):
            items.append((TRAIL_STORE, e.get("Name", ""), e["EventDataStoreArn"],
                          f"Lake event data store, {(e.get('Status') or '').lower()}"))
    except ClientError:
        pass
    tags = {}
    for batch in chunks([i[2] for i in items], 20):  # API limit: 20 per call
        try:
            for rt in ct.list_tags(ResourceIdList=batch).get("ResourceTagList", []):
                tags[rt["ResourceId"]] = tags_to_dict(rt.get("TagsList"))
        except ClientError:
            pass
    return [Res(c.region, rtype, name, arn, tags=tags.get(arn, {}), via="cloudtrail", detail=detail)
            for rtype, name, arn, detail in items]


def kms_keys(c):
    kms = c.client("kms")
    aliases = defaultdict(list)
    for a in paged(kms, "list_aliases", "Aliases"):
        if a.get("TargetKeyId"):
            aliases[a["TargetKeyId"]].append(a["AliasName"].split("/", 1)[-1])
    out = []
    for k in paged(kms, "list_keys", "Keys"):
        meta = kms.describe_key(KeyId=k["KeyId"])["KeyMetadata"]
        if meta.get("KeyManager") != "CUSTOMER":  # AWS managed keys are free and can't be tagged
            continue
        try:
            tags = {t["TagKey"]: t.get("TagValue", "")
                    for t in paged(kms, "list_resource_tags", "Tags", KeyId=k["KeyId"])}
        except ClientError:
            tags = {}
        out.append(Res(c.region, KMS_KEY, k["KeyId"], k["KeyArn"], tags=tags, via="kms",
                       name=tags.get("Name") or ", ".join(aliases.get(k["KeyId"], [])),
                       detail=f"{meta.get('KeySpec', '')} {meta.get('KeyUsage', '')}, {meta.get('KeyState', '')}"))
    return out


def sqs_queues(c):
    sqs = c.client("sqs")
    out = []
    for url in paged(sqs, "list_queues", "QueueUrls"):
        name = url.rstrip("/").split("/")[-1]
        try:
            tags = sqs.list_queue_tags(QueueUrl=url).get("Tags") or {}
        except ClientError:
            tags = {}
        out.append(Res(c.region, SQS_QUEUE, name, c.arn("sqs", name), tags=tags, via="sqs",
                       detail="FIFO queue" if name.endswith(".fifo") else "standard queue", extra={"url": url}))
    return out


def sns_topics(c):
    sns = c.client("sns")
    out = []
    for t in paged(sns, "list_topics", "Topics"):
        arn = t["TopicArn"]
        try:
            tags = tags_to_dict(sns.list_tags_for_resource(ResourceArn=arn).get("Tags"))
        except ClientError:
            tags = {}
        out.append(Res(c.region, SNS_TOPIC, arn.split(":")[-1], arn, tags=tags, via="sns"))
    return out


def ses_resources(c):
    ses = c.client("sesv2")

    def tags_for(arn):
        try:
            return tags_to_dict(ses.list_tags_for_resource(ResourceArn=arn).get("Tags"))
        except ClientError:
            return {}

    out = []
    for i in token_paged(ses.list_email_identities, "EmailIdentities", PageSize=100):
        name = i["IdentityName"]
        arn = c.arn("ses", f"identity/{name}")
        status = {"SUCCESS": "verified"}.get(i.get("VerificationStatus"), (i.get("VerificationStatus") or "").lower())
        out.append(Res(c.region, SES_IDENTITY, name, arn, tags=tags_for(arn), via="ses",
                       detail=f"{(i.get('IdentityType') or '').replace('_', ' ').lower()}, {status}"))
    for name in token_paged(ses.list_configuration_sets, "ConfigurationSets", PageSize=100):
        arn = c.arn("ses", f"configuration-set/{name}")
        out.append(Res(c.region, SES_CONFIG, name, arn, tags=tags_for(arn), via="ses"))
    return out


def sms_resources(c):
    """AWS End User Messaging SMS (the pinpoint-sms-voice-v2 API)."""
    sms = c.client("pinpoint-sms-voice-v2")

    def tags_for(arn):
        try:
            return tags_to_dict(sms.list_tags_for_resource(ResourceArn=arn).get("Tags"))
        except ClientError:
            return {}

    out = []
    for n in paged(sms, "describe_phone_numbers", "PhoneNumbers"):
        arn = n["PhoneNumberArn"]
        price = n.get("MonthlyLeasingPrice")
        out.append(Res(c.region, SMS_NUMBER, n.get("PhoneNumberId") or arn.split("/")[-1], arn,
                       tags=tags_for(arn), via="sms",
                       detail=f"{n.get('PhoneNumber', '')} {n.get('NumberType', '')}, {n.get('Status', '')}"
                              + (f", {price}/month lease" if price else "")))
    for s in paged(sms, "describe_sender_ids", "SenderIds"):
        arn = s["SenderIdArn"]
        out.append(Res(c.region, SMS_SENDER, f"{s['SenderId']} ({s.get('IsoCountryCode', '')})", arn,
                       tags=tags_for(arn), via="sms"))
    for p in paged(sms, "describe_pools", "Pools"):
        arn = p["PoolArn"]
        out.append(Res(c.region, SMS_POOL, p["PoolId"], arn, tags=tags_for(arn), via="sms",
                       detail=p.get("Status", "")))
    for cs in paged(sms, "describe_configuration_sets", "ConfigurationSets"):
        arn = cs["ConfigurationSetArn"]
        out.append(Res(c.region, SMS_CONFIG, cs["ConfigurationSetName"], arn, tags=tags_for(arn), via="sms"))
    return out


def mediaconvert_resources(c):
    mc = c.client("mediaconvert")
    endpoint = None
    try:  # older accounts use a per-account endpoint; newer SDKs work without it
        eps = mc.describe_endpoints(MaxResults=1).get("Endpoints") or []
        if eps:
            endpoint = eps[0]["Url"]
            mc = c.session.client("mediaconvert", region_name=c.region, endpoint_url=endpoint, config=CFG)
    except (ClientError, BotoCoreError, AttributeError):
        pass

    def tags_for(arn):
        try:
            return (mc.list_tags_for_resource(Arn=arn).get("ResourceTags") or {}).get("Tags") or {}
        except ClientError:
            return {}

    def res(rtype, item, detail):
        return Res(c.region, rtype, item["Name"], item["Arn"], tags=tags_for(item["Arn"]), via="mediaconvert",
                   detail=detail, extra={"endpoint": endpoint})

    out = []
    for q in paged(mc, "list_queues", "Queues"):
        out.append(res(MC_QUEUE, q, f"{(q.get('PricingPlan') or '').lower()} queue, {q.get('Status', '')}"))
    for t in paged(mc, "list_job_templates", "JobTemplates"):
        if t.get("Type") != "SYSTEM":
            out.append(res(MC_TEMPLATE, t, t.get("Category") or ""))
    for p in paged(mc, "list_presets", "Presets"):
        if p.get("Type") != "SYSTEM":
            out.append(res(MC_PRESET, p, p.get("Category") or ""))
    return out


def storage_gateways(c):
    sgw = c.client("storagegateway")

    def tags_for(arn):
        try:
            return tags_to_dict(sgw.list_tags_for_resource(ResourceARN=arn).get("Tags"))
        except ClientError:
            return {}

    out = []
    for g in paged(sgw, "list_gateways", "Gateways"):
        arn = g["GatewayARN"]
        out.append(Res(c.region, SGW, g.get("GatewayId") or arn.split("/")[-1], arn, tags=tags_for(arn),
                       name=g.get("GatewayName", ""), via="sgw",
                       detail=f"{g.get('GatewayType', '')}, {g.get('GatewayOperationalState', '')}"))
    for v in paged(sgw, "list_volumes", "VolumeInfos"):
        arn = v["VolumeARN"]
        r = Res(c.region, SGW_VOLUME, v.get("VolumeId") or arn.split("/")[-1], arn, tags=tags_for(arn),
                via="sgw", detail=f"{v.get('VolumeType', '')}, {v.get('VolumeSizeInBytes', 0) / 1024 ** 3:.0f} GiB")
        gid = v.get("GatewayId") or (v.get("GatewayARN") or "").split("/")[-1]
        if gid:
            r.relations.append(("res", gid, "on gateway"))
        out.append(r)
    for fs in paged(sgw, "list_file_shares", "FileShareInfoList"):
        arn = fs["FileShareARN"]
        r = Res(c.region, SGW_SHARE, fs.get("FileShareId") or arn.split("/")[-1], arn, tags=tags_for(arn),
                via="sgw", detail=f"{fs.get('FileShareType', '')} share, {fs.get('FileShareStatus', '')}")
        gid = (fs.get("GatewayARN") or "").split("/")[-1]
        if gid:
            r.relations.append(("res", gid, "on gateway"))
        out.append(r)
    return out


def log_groups(c):
    logs = c.client("logs")
    out = []
    for g in paged(logs, "describe_log_groups", "logGroups"):
        name = g["logGroupName"]
        arn = g.get("logGroupArn") or g["arn"]
        arn = arn[:-2] if arn.endswith(":*") else arn
        try:
            tags = logs.list_tags_for_resource(resourceArn=arn).get("tags") or {}
        except AttributeError:  # older SDK
            tags = logs.list_tags_log_group(logGroupName=name).get("tags") or {}
        except ClientError:
            tags = {}
        size = g.get("storedBytes", 0) / 1024 ** 3
        keep = f"kept {g['retentionInDays']} days" if g.get("retentionInDays") else "kept forever"
        r = Res(c.region, LOG_GROUP, name, arn, tags=tags, via="logs", detail=f"{size:.2f} GiB stored, {keep}")
        refs = [m.group(1) for p in LOG_GROUP_PATTERNS for m in [p.match(name)] if m]
        refs += INSTANCE_ID_RE.findall(name)
        for ref in dict.fromkeys(refs):
            r.relations.append(("res", ref, "logs for"))
        out.append(r)
    return out


def cloudwatch_alarms(c):
    cw = c.client("cloudwatch")
    out = []
    for page in cw.get_paginator("describe_alarms").paginate(AlarmTypes=["MetricAlarm", "CompositeAlarm"]):
        for a in (page.get("MetricAlarms") or []) + (page.get("CompositeAlarms") or []):
            arn = a["AlarmArn"]
            try:
                tags = tags_to_dict(cw.list_tags_for_resource(ResourceARN=arn).get("Tags"))
            except ClientError:
                tags = {}
            what = f"{a.get('Namespace', '')} {a.get('MetricName', '')}".strip() or "composite alarm"
            r = Res(c.region, ALARM, a["AlarmName"], arn, tags=tags, via="cloudwatch",
                    detail=f"{what}, {a.get('StateValue', '')}")
            for d in a.get("Dimensions") or []:
                v = d.get("Value", "")
                if v.startswith(("app/", "net/", "gwy/")):  # load balancer dimension -> its name
                    v = v.split("/")[1]
                r.relations.append(("res", v, "watches"))
            out.append(r)
    return out


def guardduty_detectors(c):
    gd = c.client("guardduty")
    out = []
    for did in paged(gd, "list_detectors", "DetectorIds"):
        d = gd.get_detector(DetectorId=did)
        out.append(Res(c.region, GUARDDUTY, did, c.arn("guardduty", f"detector/{did}"),
                       tags=d.get("Tags") or {}, via="guardduty",
                       detail=f"detector, {d.get('Status', '')} (charges may not follow this tag)"))
    return out


REGIONAL_COLLECTORS = [
    ("EC2 instances", ec2_instances), ("EBS volumes", ec2_volumes),
    ("EBS snapshots", ec2_snapshots), ("Elastic IPs", ec2_addresses),
    ("load balancers", elbv2_load_balancers), ("classic load balancers", elb_classic),
    ("NAT gateways", ec2_nat_gateways), ("VPC endpoints", ec2_vpc_endpoints),
    ("AMIs", ec2_images), ("RDS clusters", rds_clusters), ("RDS instances", rds_instances),
    ("Lambda functions", lambda_functions), ("DynamoDB tables", dynamodb_tables),
    ("ECS clusters/services", ecs_resources), ("EKS clusters", eks_clusters),
    ("ElastiCache clusters", elasticache_clusters), ("EFS file systems", efs_file_systems),
    ("Secrets Manager secrets", secrets), ("KMS keys", kms_keys), ("SQS queues", sqs_queues),
    ("SNS topics", sns_topics), ("SES identities and configuration sets", ses_resources),
    ("End User Messaging SMS resources", sms_resources), ("MediaConvert resources", mediaconvert_resources),
    ("API Gateway REST API stages", apigateway_rest_stages),
    ("API Gateway HTTP/WebSocket API stages", apigateway_v2_stages),
    ("Cognito user pools", cognito_user_pools), ("ECR repositories", ecr_repositories),
    ("SSM advanced parameters", ssm_parameters), ("EventBridge event buses", event_buses),
    ("IVS channels", ivs_channels), ("IVS stages", ivs_stages), ("IVS chat rooms", ivs_chat_rooms),
    ("Security Hub", security_hub), ("Config rules and recorder", config_resources),
    ("CloudTrail trails and event data stores", cloudtrail_resources),
    ("OpenSearch domains", opensearch_domains), ("OpenSearch Serverless collections", opensearch_collections),
    ("Redshift clusters", redshift_clusters), ("Redshift Serverless", redshift_serverless),
    ("EMR clusters", emr_clusters), ("EMR Serverless applications", emr_serverless_apps),
    ("Data Pipeline pipelines", data_pipelines), ("Transfer Family servers and web apps", transfer_resources),
    ("QuickSight users", quicksight_users), ("X-Ray groups", xray_groups),
    ("WAF web ACLs and rule groups", waf_regional), ("Security Lake", security_lake),
    ("CloudSearch domains", cloudsearch_domains),
    ("Storage Gateway resources", storage_gateways),
    ("CloudWatch log groups", log_groups), ("CloudWatch alarms", cloudwatch_alarms),
    ("GuardDuty detectors", guardduty_detectors),
]


def s3_buckets(session, part, regions, tag_indexes):
    s3 = session.client("s3", config=CFG)
    if s3.can_paginate("list_buckets"):
        buckets = [b for p in s3.get_paginator("list_buckets").paginate() for b in p.get("Buckets", [])]
    else:
        buckets = s3.list_buckets().get("Buckets", [])
    out = []
    for b in buckets:
        name = b["Name"]
        region = b.get("BucketRegion")
        if not region:
            try:
                loc = s3.get_bucket_location(Bucket=name).get("LocationConstraint")
            except ClientError:
                continue
            region = {None: "us-east-1", "": "us-east-1", "EU": "eu-west-1"}.get(loc, loc)
        if region not in regions:
            continue
        arn = f"arn:{part}:s3:::{name}"
        out.append(Res(region, S3, name, arn, tags=tag_indexes.get(region, {}).get(arn, {}),
                       detail=f"created {b['CreationDate']:%Y-%m-%d}"))
    return out


def cloudfront_distributions(session, tag_index):
    cf = session.client("cloudfront", config=CFG)
    out = []
    for page in cf.get_paginator("list_distributions").paginate():
        for d in (page.get("DistributionList") or {}).get("Items") or []:
            aliases = (d.get("Aliases") or {}).get("Items") or [d["DomainName"]]
            origins = [o["DomainName"] for o in (d.get("Origins") or {}).get("Items") or []]
            detail = ", ".join(aliases) + (f" -> {', '.join(origins)}" if origins else "")
            out.append(Res("global", CLOUDFRONT, d["Id"], d["ARN"], tags=tag_index.get(d["ARN"], {}),
                           detail=detail, api_region="us-east-1"))
    return out


def route53_resources(session, part):
    r53 = session.client("route53", region_name="us-east-1", config=CFG)

    def tag_map(rtype, ids):
        found = {}
        for batch in chunks(ids, 10):  # API limit: 10 IDs per call
            try:
                for s in r53.list_tags_for_resources(ResourceType=rtype, ResourceIds=batch).get("ResourceTagSets", []):
                    found[s["ResourceId"]] = tags_to_dict(s.get("Tags"))
            except ClientError:
                pass
        return found

    out = []
    zones = list(paged(r53, "list_hosted_zones", "HostedZones"))
    ztags = tag_map("hostedzone", [z["Id"].split("/")[-1] for z in zones])
    for z in zones:
        zid = z["Id"].split("/")[-1]
        tags = ztags.get(zid, {})
        private = (z.get("Config") or {}).get("PrivateZone")
        out.append(Res("global", R53_ZONE, zid, f"arn:{part}:route53:::hostedzone/{zid}", tags=tags,
                       name=tags.get("Name") or z["Name"].rstrip("."), via="route53", api_region="us-east-1",
                       detail=f"{'private' if private else 'public'} zone, {z.get('ResourceRecordSetCount', '?')} records"))
    checks = list(paged(r53, "list_health_checks", "HealthChecks"))
    ctags = tag_map("healthcheck", [h["Id"] for h in checks])
    for h in checks:
        hc = h.get("HealthCheckConfig") or {}
        target = hc.get("FullyQualifiedDomainName") or hc.get("IPAddress") or ""
        out.append(Res("global", R53_CHECK, h["Id"], f"arn:{part}:route53:::healthcheck/{h['Id']}",
                       tags=ctags.get(h["Id"], {}), via="route53", api_region="us-east-1",
                       detail=f"{hc.get('Type', '')} check on {target}".strip()))
    return out


# ---------------------------------------------------------------- linking
def haystack(r):
    """Lower-cased text that --match, --only and --exclude words are searched in."""
    return " ".join([r.rid, r.name, r.detail, *r.tags.values()]).lower()


def match_where(r, word):
    """Where a word was found on a resource, so false matches (e.g. 'dev' in 'devops') are easy to spot."""
    w = word.lower()
    if w in (r.name or "").lower():
        return f"'{word}' in name {r.name}"
    if w in r.rid.lower():
        return f"'{word}' in ID"
    for k, v in r.tags.items():
        if w in str(v).lower():
            return f"'{word}' in tag {k}={v}"
    if w in r.detail.lower():
        return f"'{word}' in details"
    return f"'{word}'"


def has_tag(tags, key, value=None):
    """True if any copy of the key (any capitalization) has the value (any capitalization)."""
    return any(k.lower() == key.lower() and (value is None or str(v).lower() == value.lower())
               for k, v in tags.items())


class State:
    """Tracks what has (or is getting) your tags so related resources can be flagged."""

    def __init__(self, match_words, tags_label=""):
        self.tags = tags_label  # e.g. "Environment=Dev, Platform=ExamplePlatform", for context lines
        # suffix for context lines; with --by-word each resource gets its own value
        self.tagged = f", tagged {tags_label}" if tags_label else ", which is getting your tags"
        self.prod_instances, self.prod_volumes, self.prod_vpcs = set(), set(), set()
        self.prod_ids = set()   # IDs/names/ARNs of every resource with your tags, any type
        self.names = {}         # resource ID -> Name, for readable context lines
        self.skipped = {}       # resource ID -> tag that got it left out by --skip-tagged
        self.match = [w.lower() for w in match_words]

    def linked_to_skipped(self, r):
        """Relations pointing at a resource that --skip-tagged left out (e.g. a Production instance)."""
        return [(verb, ref) for kind, ref, verb in dict.fromkeys(r.relations) if ref in self.skipped]

    def label(self, ref):
        """Readable name for a related resource referenced by ID, name or ARN."""
        nm = self.names.get(ref)
        if ref.startswith("arn:"):
            return nm or ref
        return f"{ref} ({nm})" if nm and nm != ref else ref

    def mark_prod(self, r):
        self.prod_ids.update((r.rid, r.arn))
        if r.rtype == INSTANCE:
            self.prod_instances.add(r.rid)
            self.prod_vpcs.update(ref for kind, ref, _ in r.relations if kind == "vpc")
        elif r.rtype == VOLUME:
            self.prod_volumes.add(r.rid)

    def assess(self, r):
        """Return (context lines, strongly linked to production?)."""
        lines, strong, seen = [], False, set()
        for rel in r.relations:
            if rel in seen:
                continue
            seen.add(rel)
            kind, ref, verb = rel
            if kind == "instance":
                nm = self.names.get(ref)
                label = f"{ref} ({nm})" if nm else ref
                if ref in self.prod_instances:
                    lines.append(green(f"{verb} {label}{self.tagged}"))
                    strong = True
                else:
                    lines.append(f"{verb} {label}")
            elif kind == "volume":
                if ref in self.prod_volumes:
                    lines.append(green(f"{verb} {ref}{self.tagged}"))
                    strong = True
                else:
                    lines.append(f"{verb} {ref}")
            elif kind == "vpc":
                if ref in self.prod_vpcs:
                    lines.append(f"{verb} {ref}, same VPC as instances " + (f"tagged {self.tags}" if self.tags else "getting your tags"))
                else:
                    lines.append(f"{verb} {ref}")
            elif kind == "res":  # any resource, matched by ID, name or ARN
                label = self.label(ref)
                if ref in self.prod_ids:
                    lines.append(green(f"{verb} {label}{self.tagged}"))
                    strong = True
                else:
                    lines.append(f"{verb} {label}")
        if self.match:
            hay = haystack(r)
            hits = [w for w in self.match if w in hay]
            if hits:
                lines.append(green("matches " + "; ".join(match_where(r, w) for w in hits)))
                strong = True
        return lines, strong


# ---------------------------------------------------------------- review
def ask(prompt):
    try:
        return input(prompt)
    except EOFError:
        return None


def ask_choice(default, can_go_back=False):
    opts = "[Y]es/[n]o" if default == "y" else "[y]es/[N]o"
    back = "  [b]ack" if can_go_back else ""
    letters = "ynatsq" + ("b" if can_go_back else "")
    while True:
        ans = ask(f"   Tag it? {opts}  [t]ags of your own{back}  [a]ll of this type  [s]kip this type  "
                  "[q]uit review: ")
        if ans is None:
            return "q"
        ans = ans.strip().lower()
        if not ans:
            return default
        if ans[0] in letters:
            return ans[0]
        print(f"   Please answer {', '.join(letters[:-1])} or {letters[-1]}.")


def ask_custom_tags(planned):
    """[t]: tags for just this resource. Returns a dict, or None to go back to the prompt."""
    print(dim(f"   Planned: {fmt_tags(planned) or '(nothing)'}. Your tags are added to these, or replace the "
              "same key."))
    while True:
        ans = ask("   Tags for this one (KEY=VALUE, separate with commas; Enter to go back): ")
        if not ans or not ans.strip():
            return None
        tags, bad = {}, []
        for part in ans.split(","):
            k, sep, v = part.partition("=")
            if not sep or not k.strip():
                bad.append(part.strip())
            else:
                tags[k.strip()] = v.strip()
        if not bad:
            return tags
        print(f"   Each tag needs KEY=VALUE (couldn't read: {', '.join(bad)}).")


def case_variants(tags, target):
    """Existing keys that differ from one of your tag keys only in capitalization, e.g. 'environment'
    next to 'Environment'. AWS treats them as separate tags, so adding yours would leave both."""
    wanted = {k.lower(): k for k in target}
    return {k: v for k, v in tags.items() if k.lower() in wanted and k != wanted[k.lower()]}


def duplicate_keys(tags):
    """Keys present in more than one capitalization, e.g. {'environment': ['Environment=Production', 'environment=qa']}."""
    groups = defaultdict(list)
    for k, v in tags.items():
        if not k.startswith("aws:"):
            groups[k.lower()].append(f"{k}={v}")
    return {k: sorted(v) for k, v in groups.items() if len(v) > 1}


def show(r, n, total, links, conflict, already, variants=None, adds_copy=True, plan=None, removing=None,
         mismatch=None):
    print()
    print(dim("-" * 76))
    print(bold(f"[{n}/{total}] {r.region}  {r.rtype}  {r.rid}"))
    print(f"   Name     : {r.name or dim('(none)')}")
    if r.detail:
        print(f"   Detail   : {r.detail}")
    other = {k: v for k, v in r.tags.items() if k != "Name" and not k.startswith("aws:")}
    print(f"   Tags     : {fmt_tags(other) or dim('(none)')}")
    for i, line in enumerate(links):
        print(f"   {'Context' if i == 0 else '':<9}: {line}")
    if plan:
        print(f"   Will set : {plan}")
    if removing:
        print(f"   Will remove: {fmt_tags(removing)}")
    if conflict:
        print(yellow(f"   Heads up : already has {fmt_tags(conflict)}; yes overwrites it"))
    if mismatch:
        print(yellow(f"   Heads up : {fmt_tags(mismatch)} says something different; yes removes it"))
    if variants and not removing and adds_copy:
        print(yellow(f"   Heads up : also has {fmt_tags(variants)} with different capitalization; AWS treats "
                     "that as a separate tag, so yes would leave both (--fix-case removes it)"))
    elif variants and not removing:
        print(dim(f"   Note     : also has {fmt_tags(variants)} with different capitalization (a separate tag "
                  "this doesn't change; --fix-case removes it)"))
    if already:
        print(dim("   Already has all of these tags."))


def review(queue, base_target, st, args, targets=None):
    selected, decisions, auto, removals, custom = [], {}, {}, {}, {}
    if args.select_all:
        auto = {t: "y" for t in TYPE_ORDER}
    total = len(queue)
    history = []  # what everything looked like at each prompt, so [b]ack can return to it

    def snapshot():
        return (list(selected), dict(decisions), dict(auto), {k: list(v) for k, v in removals.items()},
                dict(custom), set(st.prod_ids), set(st.prod_instances), set(st.prod_volumes), set(st.prod_vpcs))

    i = 0
    while i < len(queue):
        n, r = i + 1, queue[i]
        target = (targets or {}).get(r, base_target)  # with --by-word, each resource has its own value
        links, strong = st.assess(r)
        conflict = {k: r.tags[k] for k in target if k in r.tags and r.tags[k] != target[k]}
        variants = case_variants(r.tags, target)  # e.g. 'environment=qa' when tagging 'Environment'
        # --fix-case removes those other copies once yours is added, so no duplicate is left.
        removing = variants if args.fix_case else {}
        # Only a problem if tagging would add a new copy; changing an existing Environment value doesn't.
        adds_copy = not removing and any(k not in r.tags and any(v.lower() == k.lower() for v in variants)
                                         for k in target)
        # A copy that says something else (environment=qa when setting Environment=Production) needs a decision.
        mismatch = {vk: vv for vk, vv in removing.items()
                    if any(k.lower() == vk.lower() and str(vv).lower() != str(v).lower() for k, v in target.items())}
        blocking_conflict = (conflict and not args.overwrite) or mismatch
        already = has_all(r.tags, target) and not removing
        # Something attached to a resource that --skip-tagged left out (e.g. a Production
        # instance's volume) probably belongs with that resource, so it's never auto-selected.
        held = st.linked_to_skipped(r)
        for verb, ref in held:
            links = links + [yellow(f"{verb} {st.label(ref)}, which is tagged {st.skipped[ref]}")]
        default = "y" if strong and not blocking_conflict and not adds_copy and not already and not held else "n"
        # "yes to all" never silently overwrites a different existing value (unless --overwrite), adds a
        # second copy of a key in another capitalization, or tags something attached to a left-out resource.
        if r.rtype in auto and not (auto[r.rtype] == "y" and (blocking_conflict or adds_copy or held)):
            choice = auto[r.rtype]
            if choice == "y":
                why = "--select-all" if args.select_all else "yes to all of this type"
                print(dim(f"   + [{n}/{total}] {r.region} {r.rtype} {r.rid} ({why})"))
        else:
            show(r, n, total, links, conflict, already, variants, adds_copy, fmt_tags(target) if targets else None,
                 removing, mismatch)
            history.append((i, snapshot()))
            choice = ask_choice(default, can_go_back=len(history) > 1)
            if choice == "b":
                history.pop()  # this prompt
                i, state = history.pop()  # the one before it
                (selected, decisions, auto, removals, custom, st.prod_ids, st.prod_instances, st.prod_volumes,
                 st.prod_vpcs) = state
                print(yellow(f"   Going back to [{i + 1}/{total}]; your answer there (and anything after it) "
                             "is cleared."))
                continue
            if choice == "t":
                own = ask_custom_tags(target)
                if own is None:
                    history.pop()
                    continue  # show the same prompt again
                custom[r] = own
                choice = "y"
            elif choice in ("a", "s"):
                auto[r.rtype] = "y" if choice == "a" else "n"
                choice = auto[r.rtype]
            elif choice == "q":
                for rest in queue[n - 1:]:
                    decisions[rest] = "not reviewed"
                break
        if choice == "y":
            name = None
            if args.ask_name and not r.tags.get("Name") and r.rtype not in auto and "Name" not in custom.get(r, {}):
                name = (ask("   Name for it (Enter to leave unnamed): ") or "").strip() or None
            selected.append((r, name))
            if removing:
                removals[r] = list(removing)
            st.mark_prod(r)
            decisions[r] = "selected"
        else:
            decisions[r] = "skipped"
        i += 1
    return selected, decisions, removals, custom


# ---------------------------------------------------------------- apply
RETRY_WAITS = [2, 4, 8, 16, 32]  # seconds between retries of throttled resources
THROTTLE_RE = re.compile(r"throttl|rate exceeded|too high|too many requests|slow ?down", re.I)


def is_throttled(failure):
    return bool(THROTTLE_RE.search(f"{failure.get('ErrorCode', '')} {failure.get('ErrorMessage', '')}"))


def tag_native(get_client, r, tagset):
    """Tag one resource with its own service's API."""
    kv = [{"Key": k, "Value": v} for k, v in tagset.items()]
    lkv = [{"key": k, "value": v} for k, v in tagset.items()]  # services that use lower-case field names
    if r.via == "route53":
        get_client("route53", "us-east-1").change_tags_for_resource(
            ResourceType="hostedzone" if r.rtype == R53_ZONE else "healthcheck", ResourceId=r.rid, AddTags=kv)
    elif r.via == "logs":
        logs = get_client("logs", r.api_region)
        try:
            logs.tag_resource(resourceArn=r.arn, tags=tagset)
        except AttributeError:  # older SDK
            logs.tag_log_group(logGroupName=r.rid, tags=tagset)
    elif r.via == "cloudwatch":
        get_client("cloudwatch", r.api_region).tag_resource(ResourceARN=r.arn, Tags=kv)
    elif r.via == "guardduty":
        get_client("guardduty", r.api_region).tag_resource(ResourceArn=r.arn, Tags=tagset)
    elif r.via == "sgw":
        get_client("storagegateway", r.api_region).add_tags_to_resource(ResourceARN=r.arn, Tags=kv)
    elif r.via == "kms":
        get_client("kms", r.api_region).tag_resource(
            KeyId=r.arn, Tags=[{"TagKey": k, "TagValue": v} for k, v in tagset.items()])
    elif r.via == "sqs":
        get_client("sqs", r.api_region).tag_queue(QueueUrl=r.extra["url"], Tags=tagset)
    elif r.via == "sns":
        get_client("sns", r.api_region).tag_resource(ResourceArn=r.arn, Tags=kv)
    elif r.via == "ses":
        get_client("sesv2", r.api_region).tag_resource(ResourceArn=r.arn, Tags=kv)
    elif r.via == "sms":
        get_client("pinpoint-sms-voice-v2", r.api_region).tag_resource(ResourceArn=r.arn, Tags=kv)
    elif r.via == "mediaconvert":
        get_client("mediaconvert", r.api_region, r.extra.get("endpoint")).tag_resource(Arn=r.arn, Tags=tagset)
    elif r.via in ("ivs", "ivs-realtime", "ivschat"):
        get_client(r.via, r.api_region).tag_resource(resourceArn=r.arn, tags=tagset)
    elif r.via == "apigw":
        get_client("apigateway", r.api_region).tag_resource(resourceArn=r.arn, tags=tagset)
    elif r.via == "apigw2":
        get_client("apigatewayv2", r.api_region).tag_resource(ResourceArn=r.arn, Tags=tagset)
    elif r.via == "cognito":
        get_client("cognito-idp", r.api_region).tag_resource(ResourceArn=r.arn, Tags=tagset)
    elif r.via == "securityhub":
        get_client("securityhub", r.api_region).tag_resource(ResourceArn=r.arn, Tags=tagset)
    elif r.via == "config":
        get_client("config", r.api_region).tag_resource(ResourceArn=r.arn, Tags=kv)
    elif r.via == "cloudtrail":
        get_client("cloudtrail", r.api_region).add_tags(ResourceId=r.arn, TagsList=kv)
    elif r.via == "ecr":
        get_client("ecr", r.api_region).tag_resource(resourceArn=r.arn, tags=kv)
    elif r.via == "ssm":
        get_client("ssm", r.api_region).add_tags_to_resource(ResourceType="Parameter", ResourceId=r.rid, Tags=kv)
    elif r.via == "events":
        get_client("events", r.api_region).tag_resource(ResourceARN=r.arn, Tags=kv)
    elif r.via == "opensearch":
        get_client("opensearch", r.api_region).add_tags(ARN=r.arn, TagList=kv)
    elif r.via == "aoss":
        get_client("opensearchserverless", r.api_region).tag_resource(resourceArn=r.arn, tags=lkv)
    elif r.via == "redshift":
        get_client("redshift", r.api_region).create_tags(ResourceName=r.arn, Tags=kv)
    elif r.via == "redshift-serverless":
        get_client("redshift-serverless", r.api_region).tag_resource(resourceArn=r.arn, tags=lkv)
    elif r.via == "emr":
        get_client("emr", r.api_region).add_tags(ResourceId=r.rid, Tags=kv)
    elif r.via == "emr-serverless":
        get_client("emr-serverless", r.api_region).tag_resource(resourceArn=r.arn, tags=tagset)
    elif r.via == "datapipeline":
        get_client("datapipeline", r.api_region).add_tags(pipelineId=r.rid, tags=lkv)
    elif r.via == "transfer":
        get_client("transfer", r.api_region).tag_resource(Arn=r.arn, Tags=kv)
    elif r.via == "quicksight":
        get_client("quicksight", r.api_region).tag_resource(ResourceArn=r.arn, Tags=kv)
    elif r.via == "xray":
        get_client("xray", r.api_region).tag_resource(ResourceARN=r.arn, Tags=kv)
    elif r.via == "wafv2":
        get_client("wafv2", r.api_region).tag_resource(ResourceARN=r.arn, Tags=kv)
    elif r.via == "securitylake":
        get_client("securitylake", r.api_region).tag_resource(resourceArn=r.arn, tags=lkv)
    else:
        raise ValueError(f"no tagging method for {r.rtype}")


def untag_native(get_client, r, keys):
    """Remove tag keys from one resource with its own service's API (used by --fix-case)."""
    c = lambda svc: get_client(svc, r.api_region)  # noqa: E731
    v = r.via
    if v == "ec2":
        c("ec2").delete_tags(Resources=[r.rid], Tags=[{"Key": k} for k in keys])
    elif v == "arn":
        failed = c("resourcegroupstaggingapi").untag_resources(ResourceARNList=[r.arn], TagKeys=keys).get(
            "FailedResourcesMap") or {}
        if r.arn in failed:
            raise ValueError(failed[r.arn].get("ErrorMessage") or failed[r.arn].get("ErrorCode"))
    elif v == "route53":
        get_client("route53", "us-east-1").change_tags_for_resource(
            ResourceType="hostedzone" if r.rtype == R53_ZONE else "healthcheck", ResourceId=r.rid, RemoveTagKeys=keys)
    elif v == "logs":
        try:
            c("logs").untag_resource(resourceArn=r.arn, tagKeys=keys)
        except AttributeError:  # older SDK
            c("logs").untag_log_group(logGroupName=r.rid, tags=keys)
    elif v == "sgw":
        c("storagegateway").remove_tags_from_resource(ResourceARN=r.arn, TagKeys=keys)
    elif v == "kms":
        c("kms").untag_resource(KeyId=r.arn, TagKeys=keys)
    elif v == "sqs":
        c("sqs").untag_queue(QueueUrl=r.extra["url"], TagKeys=keys)
    elif v == "mediaconvert":
        get_client("mediaconvert", r.api_region, r.extra.get("endpoint")).untag_resource(Arn=r.arn, TagKeys=keys)
    elif v == "cloudtrail":
        c("cloudtrail").remove_tags(ResourceId=r.arn, TagsList=[{"Key": k} for k in keys])
    elif v == "ssm":
        c("ssm").remove_tags_from_resource(ResourceType="Parameter", ResourceId=r.rid, TagKeys=keys)
    elif v == "opensearch":
        c("opensearch").remove_tags(ARN=r.arn, TagKeys=keys)
    elif v == "redshift":
        c("redshift").delete_tags(ResourceName=r.arn, TagKeys=keys)
    elif v == "emr":
        c("emr").remove_tags(ResourceId=r.rid, TagKeys=keys)
    elif v == "datapipeline":
        c("datapipeline").remove_tags(pipelineId=r.rid, tagKeys=keys)
    elif v == "transfer":
        c("transfer").untag_resource(Arn=r.arn, TagKeys=keys)
    elif v in ("ivs", "ivs-realtime", "ivschat"):
        c(v).untag_resource(resourceArn=r.arn, tagKeys=keys)
    elif v in ("apigw", "ecr", "aoss", "redshift-serverless", "emr-serverless", "securitylake"):
        svc = {"apigw": "apigateway", "aoss": "opensearchserverless"}.get(v, v)
        c(svc).untag_resource(resourceArn=r.arn, tagKeys=keys)
    elif v in ("cloudwatch", "events", "xray", "wafv2"):
        c(v).untag_resource(ResourceARN=r.arn, TagKeys=keys)
    elif v in ("guardduty", "sns", "ses", "sms", "apigw2", "cognito", "securityhub", "config", "quicksight"):
        svc = {"ses": "sesv2", "sms": "pinpoint-sms-voice-v2", "apigw2": "apigatewayv2",
               "cognito": "cognito-idp"}.get(v, v)
        c(svc).untag_resource(ResourceArn=r.arn, TagKeys=keys)
    else:
        raise ValueError(f"no way to remove tags from {r.rtype}")


def remove_variants(session, removals, results):
    """--fix-case: after a resource got your tag, remove its differently-capitalized copies."""
    clients = {}

    def get_client(svc, region, endpoint=None):
        key = (svc, region, endpoint)
        if key not in clients:
            clients[key] = session.client(svc, region_name=region, endpoint_url=endpoint, config=CFG)
        return clients[key]

    for r, keys in removals.items():
        if results.get(r) != "tagged":
            continue  # never remove the old copy unless the new one is in place
        try:
            untag_native(get_client, r, keys)
            results[r] = f"tagged; removed {', '.join(keys)}"
        except (ClientError, BotoCoreError, ValueError) as e:
            results[r] = f"tagged, but couldn't remove {', '.join(keys)}: {err_text(e)}"


def apply_tags(session, selected, target, targets=None):
    results = {}
    clients = {}

    def get_client(svc, region, endpoint=None):
        key = (svc, region, endpoint)
        if key not in clients:
            clients[key] = session.client(svc, region_name=region, endpoint_url=endpoint, config=CFG)
        return clients[key]

    groups = defaultdict(list)
    for r, name in selected:
        tagset = dict((targets or {}).get(r, target))
        if name:
            tagset["Name"] = name
        groups[(r.via, r.api_region, tuple(sorted(tagset.items())))].append(r)

    for (via, region, items), group in groups.items():
        tagset = dict(items)
        if via == "ec2":
            ec2 = session.client("ec2", region_name=region, config=CFG)
            ec2_tags = [{"Key": k, "Value": v} for k, v in tagset.items()]
            for batch in chunks(group, 100):
                try:
                    ec2.create_tags(Resources=[r.rid for r in batch], Tags=ec2_tags)
                    results.update({r: "tagged" for r in batch})
                except ClientError:
                    for r in batch:  # one bad ID fails the whole call, so retry one by one
                        try:
                            ec2.create_tags(Resources=[r.rid], Tags=ec2_tags)
                            results[r] = "tagged"
                        except ClientError as e:
                            results[r] = f"FAILED: {e.response['Error'].get('Message', e)}"
        elif via != "arn":  # services tagged through their own API, one at a time
            for r in group:
                try:
                    tag_native(get_client, r, tagset)
                    results[r] = "tagged"
                except (ClientError, BotoCoreError, ValueError) as e:
                    results[r] = f"FAILED: {err_text(e)}"
        else:
            tg = session.client("resourcegroupstaggingapi", region_name=region, config=CFG)
            # The tagging API reports throttling per resource instead of raising an error, so the
            # SDK's own retries never see it. Throttled resources are retried here with backoff,
            # in smaller batches, before being reported as failed.
            pending, batch_size = list(group), 20  # API limit: 20 ARNs per call
            for round_no in range(len(RETRY_WAITS) + 1):
                throttled = []
                for batch in chunks(pending, batch_size):
                    try:
                        failed = tg.tag_resources(ResourceARNList=[r.arn for r in batch],
                                                  Tags=tagset).get("FailedResourcesMap") or {}
                    except ClientError as e:
                        err = e.response.get("Error", {})
                        failed = {r.arn: {"ErrorCode": err.get("Code", ""), "ErrorMessage": err.get("Message", str(e))}
                                  for r in batch}
                    for r in batch:
                        f = failed.get(r.arn)
                        if not f:
                            results[r] = "tagged"
                        elif is_throttled(f) and round_no < len(RETRY_WAITS):
                            throttled.append(r)
                        else:
                            results[r] = f"FAILED: {f.get('ErrorMessage') or f.get('ErrorCode')}"
                if not throttled:
                    break
                wait = RETRY_WAITS[round_no]
                print(dim(f"  AWS is throttling {len(throttled)} resource(s) in {region}; retrying in {wait}s ..."),
                      flush=True)
                time.sleep(wait)
                pending, batch_size = throttled, 5
    return results


def write_log(path, resources, decisions, selected, results, target, targets=None, removals=None):
    names = {r: name for r, name in selected if name}
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["region", "type", "id", "name", "decision", "result", "tags_requested",
                    "same_key_twice", "arn", "tags_before", "how"])
        for r in resources:
            decision = decisions.get(r, "already tagged")
            requested, before, how = "", "", ""
            if decision == "selected":
                wanted = {**(targets or {}).get(r, target), **({"Name": names[r]} if r in names else {})}
                requested = fmt_tags(wanted, limit=500)
                # What each touched key held before this run (null = it wasn't there), for --undo.
                touched = list(wanted) + list((removals or {}).get(r, []))
                before = json.dumps({k: r.tags.get(k) for k in touched})
                how = json.dumps({"via": r.via, "api_region": r.api_region, "extra": r.extra})
            twice = "; ".join(", ".join(v) for v in duplicate_keys(r.tags).values())
            w.writerow([r.region, r.rtype, r.rid, names.get(r, r.name), decision,
                        results.get(r, ""), requested, twice, r.arn, before, how])


def undo_from_log(session, acct, path, dry_run):
    """--undo: put back the values a previous run's decision log recorded in tags_before."""
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if rows and "tags_before" not in rows[0]:
        sys.exit("This decision log is from an older version of the script, which didn't record the previous "
                 "values, so it can't be undone automatically.")
    todo = []
    for row in rows:
        if not row.get("result", "").startswith("tagged") or not row.get("tags_before"):
            continue
        arn_acct = row["arn"].split(":")[4] if row["arn"].count(":") >= 5 else ""
        if arn_acct and arn_acct != acct:
            sys.exit(f"This log is for account {arn_acct}, but you're signed in to {acct}. "
                     "Use the --profile for that account.")
        how = json.loads(row["how"])
        r = Res(row["region"], row["type"], row["id"], row["arn"], name=row.get("name", ""), via=how["via"],
                api_region=how["api_region"], extra=how.get("extra") or {})
        before = json.loads(row["tags_before"])
        put_back = {k: v for k, v in before.items() if v is not None}
        remove = [k for k, v in before.items() if v is None]
        # if this run's extra copy couldn't be removed, it was never removed, so don't re-add it
        if "couldn't remove" in row["result"]:
            put_back = {k: v for k, v in put_back.items() if k in row["tags_requested"]}
        todo.append((r, put_back, remove))
    if not todo:
        print("Nothing in this log was changed, so there's nothing to undo.")
        return 0

    print(bold(f"\nUndo {len(todo)} change(s) from {path}:"))
    for r, put_back, remove in todo:
        parts = [f"set {fmt_tags(put_back)}"] if put_back else []
        parts += [f"remove {', '.join(remove)}"] if remove else []
        print(f"  {r.region:<12} {r.rtype:<27} {r.rid[:40]:<40} {r.name[:24]:<24} {'; '.join(parts)}")
    if dry_run:
        print(yellow("\nDry run: nothing was changed."))
        return 0
    if (ask("\nType 'yes' to undo these changes (anything else cancels): ") or "").strip().lower() != "yes":
        print("Cancelled. No changes made.")
        return 0

    sets = [(r, None) for r, put_back, _ in todo if put_back]
    results = apply_tags(session, sets, {}, {r: put_back for r, put_back, _ in todo if put_back})
    removals = {r: remove for r, _, remove in todo if remove}
    for r in removals:
        results.setdefault(r, "tagged")  # nothing to put back first, so go straight to removing
    remove_variants(session, removals, results)
    ok = sum(1 for v in results.values() if v.startswith("tagged") and "couldn't" not in v)
    print(green(f"Undid {ok} of {len(todo)}."))
    for r, v in results.items():
        if not v.startswith("tagged") or "couldn't" in v:
            print(yellow(f"  {r.region} {r.rtype} {r.rid}: {v}"))
    return 0


# ---------------------------------------------------------------- billing view
def show_billed_services(session, acct):
    end = dt.date.today()
    start = end - dt.timedelta(days=30)
    try:
        ce = session.client("ce", region_name="us-east-1", config=CFG)
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": str(start), "End": str(end)}, Granularity="MONTHLY",
            Metrics=["UnblendedCost"], GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            Filter={"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [acct]}})
    except Exception as e:  # optional extra; never block the main job
        print(yellow(f"Couldn't read Cost Explorer ({str(e).splitlines()[0][:160]}). Continuing.\n"))
        return
    totals, unit = defaultdict(float), "USD"
    for period in resp.get("ResultsByTime", []):
        for g in period.get("Groups", []):
            m = g["Metrics"]["UnblendedCost"]
            totals[g["Keys"][0]] += float(m["Amount"])
            unit = m.get("Unit", unit)
    print(bold(f"\nBilled to account {acct}, {start} to {end}:"))
    for svc, amt in sorted(totals.items(), key=lambda kv: -kv[1]):
        if amt < 0.01:
            continue
        fixed = next((k for k in NOT_TAGGABLE if k in svc.lower()), None)
        manual = next((k for k in MANUAL_ONLY if k in svc.lower()), None)
        key = next((k for k in COVERAGE if k in svc.lower()), None)
        if fixed is not None:
            mark = dim(NOT_TAGGABLE[fixed])
        elif manual is not None:
            mark = yellow("by hand") + dim(f" ({MANUAL_ONLY[manual]})")
        elif key is not None:
            mark = green("scanned") + (dim(f" ({COVERAGE[key]})") if COVERAGE[key] else "")
        else:
            mark = yellow("not scanned - use Tag Editor")
        print(f"  {amt:>11,.2f} {unit}  {svc[:44]:<44}  {mark}")
    print(dim("  (Cost Explorer charges $0.01 per API call; this was one call.)\n"))


# ---------------------------------------------------------------- main
def parse_args():
    p = argparse.ArgumentParser(
        description="Interactively tag billable AWS resources.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--tag", action="append", metavar="KEY=VALUE",
                   help="tag to add; repeat for each tag and quote values with spaces "
                        f"(default: {fmt_tags(DEFAULT_TAGS)})")
    g = p.add_mutually_exclusive_group()
    p.add_argument("--profile", metavar="NAME",
                   help="AWS CLI profile to use when running on your own computer (not needed in CloudShell)")
    g.add_argument("--regions", nargs="+", metavar="REGION",
                   help="regions to scan (default: the profile's region, or CloudShell's current region)")
    g.add_argument("--all-regions", action="store_true", help="scan every region enabled for the account")
    p.add_argument("--match", nargs="+", default=[], metavar="WORD",
                   help="suggest Yes for resources whose name, ID, tags or details contain any of these words")
    p.add_argument("--by-word", nargs="+", default=[], metavar="WORD=VALUE",
                   help="set the Environment tag (or --by-word-key) from a word in each resource's name, ID or tags, "
                        "e.g. qa=qa dev=dev test=test; only resources containing one of the words are included, "
                        "plus what's attached to them")
    p.add_argument("--by-word-key", default="Environment", metavar="KEY",
                   help="tag key that --by-word sets (default: Environment)")
    p.add_argument("--only", nargs="+", default=[], metavar="WORD",
                   help="keep only resources whose name, ID, tags or details contain any of these words, plus "
                        "resources attached to them (not case-sensitive)")
    p.add_argument("--types", nargs="+", default=[], metavar="TYPE",
                   help="only review these resource types, e.g. \"API Gateway stage\" or \"EC2 instance\" "
                        "(partial names work, not case-sensitive)")
    p.add_argument("--exclude", nargs="+", default=[], metavar="WORD",
                   help="leave out resources whose name, ID, tags or details contain any of these words "
                        "(not case-sensitive)")
    p.add_argument("--skip-tagged", nargs="+", default=[], metavar="KEY[=VALUE]",
                   help="leave out resources that already have this tag, e.g. Environment=Production "
                        "(or just a key, e.g. Environment, to leave out any value; not case-sensitive)")
    p.add_argument("--only-tagged", nargs="+", default=[], metavar="KEY[=VALUE]",
                   help="keep only resources that already have this tag, e.g. Environment=Production "
                        "(not case-sensitive); the opposite of --skip-tagged")
    p.add_argument("--select-all", action="store_true",
                   help="answer yes for every resource without prompting (it still asks before overwriting a "
                        "different existing value), then show the full list to confirm")
    p.add_argument("--overwrite", action="store_true",
                   help="let --select-all and 'a' replace a different existing value (e.g. Production -> Dev) "
                        "without stopping to ask")
    p.add_argument("--fix-case", action="store_true",
                   help="when a resource also has one of your tag keys in another capitalization (e.g. environment "
                        "next to Environment), remove that copy after adding yours")
    p.add_argument("--include-tagged", action="store_true", help="also show resources that already have the tags")
    p.add_argument("--ask-name", action="store_true", help="ask for a Name tag on selected resources that lack one")
    p.add_argument("--billed-services", action="store_true",
                   help="first list services billed in the last 30 days and whether this script scans them")
    p.add_argument("--dry-run", action="store_true", help="review and log only; change nothing")
    p.add_argument("--undo", metavar="LOG.csv",
                   help="undo the changes recorded in a decision log: put back the previous values and remove "
                        "tags that weren't there before (use with the same --profile)")
    return p.parse_args()


def parse_tags(pairs):
    if not pairs:
        return dict(DEFAULT_TAGS)
    tags = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            sys.exit(f"--tag must look like KEY=VALUE (got {pair!r})")
        tags[key.strip()] = value.strip()
    return tags


def resolve_regions(args, session):
    if args.regions:
        return args.regions
    if args.all_regions:
        ec2 = session.client("ec2", region_name=session.region_name or "us-east-1", config=CFG)
        return sorted(r["RegionName"] for r in ec2.describe_regions()["Regions"])
    return [session.region_name or "us-east-1"]


APPLYING = False


def main():
    global APPLYING
    args = parse_args()
    target = parse_tags(args.tag)
    by_word, by_key = [], args.by_word_key
    if args.by_word:
        if args.only:
            sys.exit("Use either --by-word or --only, not both (--by-word already limits the run to its words).")
        for pair in args.by_word:
            word, sep, value = pair.partition("=")
            if not sep or not word.strip() or not value.strip():
                sys.exit(f"--by-word must look like WORD=VALUE (got {pair!r})")
            by_word.append((word.strip().lower(), value.strip()))
        # Other tags come only from --tag here; the defaults (e.g. Platform=ExamplePlatform) aren't added.
        target = parse_tags(args.tag) if args.tag else {}
        target.pop(by_key, None)
        args.only = [w for w, _ in by_word]
    by_value = dict(by_word)
    targets = {}  # per-resource tags when --by-word is used

    def tgt(r):
        return targets.get(r, target)
    try:
        session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
        ident = session.client("sts", config=CFG).get_caller_identity()
    except (BotoCoreError, ClientError) as e:
        login = f"aws sso login --profile {args.profile}" if args.profile else "aws sso login --profile NAME"
        sys.exit(f"Couldn't sign in to AWS: {err_text(e)}\n"
                 f"If you're on your own computer, sign in first with:  {login}\n"
                 f"(and pass --profile NAME to this script if you didn't).")
    acct, part = ident["Account"], ident["Arn"].split(":")[1]
    if args.undo:
        print(bold("\nUndo a previous run"))
        print(f"  Account : {acct}  ({ident['Arn']})")
        return undo_from_log(session, acct, args.undo, args.dry_run)
    regions = resolve_regions(args, session)

    print(bold("\nInteractive resource tagging"))
    print(f"  Account : {acct}  ({ident['Arn']})")
    print(f"  Regions : {', '.join(regions)}")
    if by_word:
        print(f"  Tags    : {by_key} = " + ", ".join(f"{v} where it says '{w}'" for w, v in by_word)
              + (f"; plus {fmt_tags(target)}" if target else ""))
    else:
        print(f"  Tags    : {fmt_tags(target)}")
    if args.types:
        print(f"  Types   : {', '.join(args.types)}")
    if args.only_tagged:
        print(f"  Only    : resources currently tagged {' or '.join(args.only_tagged)}")
    if args.only:
        print(f"  Only    : resources containing {', '.join(repr(w) for w in args.only)}, and what's attached to them")
    if args.overwrite:
        print("  Replace : existing values are replaced without asking")
    if args.fix_case:
        print("  Fix case: other capitalizations of your tag keys (e.g. 'environment') are removed")
    if args.exclude:
        print(f"  Exclude : anything containing {', '.join(repr(w) for w in args.exclude)}")
    if args.skip_tagged:
        print(f"  Skip    : anything already tagged {' or '.join(args.skip_tagged)}")
    if args.select_all:
        print("  Select  : yes to every resource (you still confirm the full list at the end)")
    print(f"  Mode    :{yellow('DRY RUN - nothing will be changed') if args.dry_run else 'changes applied only after you confirm'}")
    print()

    if args.billed_services:
        show_billed_services(session, acct)

    resources, tag_indexes = [], {}
    for region in regions:
        print(f"Scanning {region} ...", flush=True)
        tag_indexes[region] = attempt("existing tags", region, build_tag_index, session, region, default={})
        ctx = Ctx(session, region, acct, part, tag_indexes[region])
        for label, fn in REGIONAL_COLLECTORS:
            resources += attempt(label, region, fn, ctx)
    print("Scanning S3" + (", CloudFront, Route 53 and WAF for CloudFront" if "us-east-1" in regions else "") + " ...",
          flush=True)
    resources += attempt("S3 buckets", "global", s3_buckets, session, part, regions, tag_indexes)
    if "us-east-1" in regions:  # global services are shown when us-east-1 is scanned
        resources += attempt("CloudFront distributions", "global", cloudfront_distributions,
                             session, tag_indexes["us-east-1"])
        resources += attempt("Route 53 zones and health checks", "global", route53_resources, session, part)
        resources += attempt("WAF web ACLs for CloudFront", "global", waf_cloudfront, session)

    rorder = {r: i for i, r in enumerate(regions)}
    resources.sort(key=lambda r: (rorder.get(r.region, len(regions)), TYPE_ORDER.index(r.rtype)))

    # --only words also count as match words, so each prompt shows which word it matched.
    st = State(args.match + [w for w in args.only if w.lower() not in (m.lower() for m in args.match)],
               "" if by_word else fmt_tags(target))
    for r in resources:
        if r.name:
            st.names.setdefault(r.rid, r.name)
        st.names.setdefault(r.arn, f"{r.rtype} {r.name or r.rid}")
        if not by_word and has_all(r.tags, target):  # with --by-word this happens once each target is known
            st.mark_prod(r)

    counts = Counter(r.rtype for r in resources)
    print(f"\nFound {len(resources)} resources -> " +
          ", ".join(f"{t}: {counts[t]}" for t in TYPE_ORDER if counts[t]))
    # Resources carrying one of your tag keys in two capitalizations (e.g. Environment and environment).
    tag_keys = {t.lower() for t in target} | ({by_key.lower()} if by_word else set())
    dupes = {r: d for r in resources for d in [{k: v for k, v in duplicate_keys(r.tags).items()
                                                 if k in tag_keys}] if d}
    if dupes:
        print(yellow(f"\n{len(dupes)} resource(s) have one of your tag keys in two capitalizations; AWS treats "
                     "them as separate tags (also listed in the decision log):"))
        for r, d in list(dupes.items())[:25]:
            pairs = "; ".join(", ".join(v) for v in d.values())
            print(f"  {r.region:<12} {r.rtype:<27} {r.rid[:30]:<30} {r.name[:24]:<24} {pairs}")
        if len(dupes) > 25:
            print(dim(f"  ...and {len(dupes) - 25} more."))

    # Resources with your tags but also a differently-capitalized copy of a key still come up for review.
    # (With --by-word each resource's tags depend on its word, so that check happens after matching.)
    queue = [r for r in resources if by_word or args.include_tagged or not has_all(r.tags, target) or r in dupes]
    already = len(resources) - len(queue)
    if already and not args.include_tagged:
        print(dim(f"{already} already have {fmt_tags(target)} and are skipped (--include-tagged to see them)."))

    already_has = {}
    if args.skip_tagged:
        rules = []
        for s in args.skip_tagged:
            k, sep, v = s.partition("=")
            rules.append((s, k.strip().lower(), v.strip().lower() if sep else None))
        for r in queue:
            hit = None
            for orig, k, v in rules:
                # every copy of the key, in any capitalization (e.g. Environment and environment)
                values = [str(val).lower() for key, val in r.tags.items() if key.lower() == k]
                # A resource whose copies disagree (Environment=Production, environment=qa) is kept
                # in the review, so you decide rather than it being left out silently.
                if values and (v is None or (v in values and len(set(values)) == 1)):
                    hit = orig
                    break
            if hit:
                already_has[r] = hit
        queue = [r for r in queue if r not in already_has]
        st.skipped = {ref: t for r, t in already_has.items() for ref in (r.rid, r.arn)}
        if already_has:
            by_type = Counter(r.rtype for r in already_has)
            print(f"\nLeft out {len(already_has)} resource(s) already tagged {' or '.join(args.skip_tagged)} -> " +
                  ", ".join(f"{t}: {by_type[t]}" for t in TYPE_ORDER if by_type[t]))

    other_types = []
    if args.types:
        wanted_types = [t.lower() for t in args.types]
        other_types = [r for r in queue if not any(t in r.rtype.lower() for t in wanted_types)]
        queue = [r for r in queue if r not in other_types]
        kinds = sorted({r.rtype for r in queue})
        print(f"\nKept {len(queue)} resource(s) of type {', '.join(kinds) or '(none matched)'}; "
              f"{len(other_types)} of other types left out by --types.")

    not_tagged_as = []
    if args.only_tagged:
        def wanted(r):
            for s in args.only_tagged:
                k, sep, v = s.partition("=")
                if has_tag(r.tags, k.strip(), v.strip() if sep else None):
                    return True
            return False
        not_tagged_as = [r for r in queue if not wanted(r)]
        queue = [r for r in queue if wanted(r)]
        print(f"\nKept {len(queue)} resource(s) currently tagged {' or '.join(args.only_tagged)}; "
              f"{len(not_tagged_as)} other(s) left out by --only-tagged.")

    reasons = {}  # why --only kept each resource, shown in the final list
    not_matching, ambiguous = [], {}
    if args.only:
        words = [w.lower() for w in args.only]
        ex_words = [w.lower() for w in args.exclude]
        direct = {}  # every resource that contains a word, so its attachments can be found too
        for r in resources:
            if any(x in haystack(r) for x in ex_words):
                continue  # excluded, so it doesn't pull in what's attached to it either
            hits = [w for w in words if w in haystack(r)]
            if not hits:
                continue
            hit = hits[0]
            if by_word and len({by_value[w] for w in hits}) > 1:
                hit = "?" + "+".join(hits)  # e.g. 'qa-test-db' matches two words with different values
            direct[r.rid] = direct[r.arn] = (r, hit)
            reasons[r] = "; ".join(match_where(r, w) for w in hits) if hit.startswith("?") else match_where(r, hit)
        # Follow attachments outward, e.g. a "qa" instance -> its volume -> that volume's snapshots.
        # A VPC, or a load balancer that merely routes to a matching instance, may be shared, so
        # those links don't pull a resource in.
        found = dict(direct)
        changed = True
        while changed:
            changed = False
            for r in resources:
                if r.rid in found:
                    continue
                link = next(((verb, ref) for kind, ref, verb in r.relations
                             if kind != "vpc" and verb != "routes to" and ref in found), None)
                if link:
                    # attached resources take the word of what they're attached to
                    found[r.rid] = found[r.arn] = (r, found[link[1]][1])
                    reasons[r] = f"{link[0]} {st.label(link[1])}"
                    changed = True
        kept = {r for r in queue if r.rid in found}
        linked = sum(1 for r in kept if r.rid not in direct)
        not_matching = [r for r in queue if r not in kept]
        queue = [r for r in queue if r in kept]
        print(f"\nFound {len(kept) - linked} resource(s) containing {', '.join(repr(w) for w in args.only)}"
              + (f", plus {linked} attached to them" if linked else "")
              + f"; {len(not_matching)} other(s) left out by --only.")

        if by_word:
            for r in queue:
                word = found[r.rid][1]
                if word.startswith("?"):
                    ambiguous[r] = word[1:].replace("+", "' and '")
                else:
                    targets[r] = {**target, by_key: by_value[word]}
            queue = [r for r in queue if r not in ambiguous]
            for r in resources:  # what already has its tags counts for linking suggestions
                if r in targets and has_all(r.tags, targets[r]):
                    st.mark_prod(r)
            done = [r for r in queue if has_all(r.tags, targets[r]) and r not in dupes and not args.include_tagged]
            queue = [r for r in queue if r not in done]
            already += len(done)
            if done:
                print(dim(f"{len(done)} already have the right {by_key} and are skipped."))
            if ambiguous:
                print(yellow(f"\nLeft out {len(ambiguous)} resource(s) that contain more than one of your words; "
                             f"set their {by_key} by hand:"))
                for r, w in list(ambiguous.items())[:25]:
                    print(f"  {r.region:<12} {r.rtype:<27} {r.rid[:40]:<40} {r.name[:30]}  ('{w}')")
                if len(ambiguous) > 25:
                    print(dim(f"  ...and {len(ambiguous) - 25} more; all are listed in the decision log."))

    excluded = {}
    if args.exclude:
        words = [w.lower() for w in args.exclude]
        for r in queue:
            hit = next((w for w in words if w in haystack(r)), None)
            if hit:
                excluded[r] = hit
        queue = [r for r in queue if r not in excluded]
        if excluded:
            print(f"\nExcluded {len(excluded)} resource(s) matching --exclude:")
            for r, w in list(excluded.items())[:40]:
                print(dim(f"  {r.region:<12} {r.rtype:<27} {r.rid[:40]:<40} {r.name[:30]}  ('{w}')"))
            if len(excluded) > 40:
                print(dim(f"  ...and {len(excluded) - 40} more; all are listed in the decision log."))

    # Resources that can only be tagged by hand: list the ones still missing the tags.
    manual = []
    for r in MANUAL:
        if any(w.lower() in haystack(r) for w in args.exclude):
            continue
        if args.types and not any(t.lower() in r.rtype.lower() for t in args.types):
            continue
        if args.only and not any(w.lower() in haystack(r) for w in args.only):
            continue
        if by_word:
            values = {by_value[w] for w in by_value if w in haystack(r)}
            if len(values) == 1:
                targets[r] = {**target, by_key: values.pop()}
        if not has_all(r.tags, tgt(r)):
            manual.append(r)

    def show_manual():
        if not manual:
            return
        print(yellow("\nTag these by hand; their service has no tagging API"
                     + ("" if by_word else f" ({fmt_tags(target)})") + ":"))
        for r in manual:
            plan = f"  -> {fmt_tags(tgt(r))}" if by_word else ""
            print(f"  {r.region:<12} {r.rtype:<27} {r.rid[:40]:<40} {r.detail}{plan}")
        if any(r.rtype == CLOUDSEARCH for r in manual):
            print(dim("  CloudSearch console > choose the domain > Tags tab > Manage."))

    if not queue:
        print("Nothing left to review.")
        show_manual()
        return 0

    selected, decisions, removals, custom = review(queue, target, st, args, targets)
    for r, own in custom.items():  # [t] tags typed for one resource go on top of its planned tags
        targets[r] = {**tgt(r), **own}
    decisions.update({r: f"excluded ('{w}')" for r, w in excluded.items()})
    decisions.update({r: f"left out (contains '{w}')" for r, w in ambiguous.items()})
    decisions.update({r: f"left out (already {t})" for r, t in already_has.items()})
    decisions.update({r: "left out (no --only word)" for r in not_matching})
    decisions.update({r: "left out (not tagged as --only-tagged)" for r in not_tagged_as})
    decisions.update({r: "left out (other type)" for r in other_types})
    decisions.update({r: "tag by hand in its console" for r in manual})
    resources = resources + manual  # so the decision log includes them

    log_path = Path.home() / f"tagging-log-{dt.datetime.now():%Y%m%d-%H%M%S}.csv"
    print("\n" + "=" * 76)
    if not selected:
        print("Nothing selected. No changes made.")
        show_manual()
        write_log(log_path, resources, decisions, selected, {}, target, targets, removals)
        print(f"Decision log: {log_path}")
        return 0

    if by_word:
        print(bold(f"{len(selected)} resource(s) will be tagged:"))
    else:
        print(bold(f"{len(selected)} resource(s) will get {fmt_tags(target)}:"))
    for r, name in selected:
        extra = f"  + Name={name}" if name else ""
        t = tgt(r)
        parts = []
        if by_word:  # show the value each one gets, and what it had before
            parts += [f"{k} {r.tags[k]} -> {v}" if k in r.tags else f"{k}={v}"
                      for k, v in t.items() if r.tags.get(k) != v]
        elif args.only_tagged or any(k in r.tags and r.tags[k] != v for k, v in t.items()):
            parts += [f"{k} {r.tags[k]} -> {v}" for k, v in t.items() if k in r.tags and r.tags[k] != v]
        if r in custom:
            parts.append("your tags " + fmt_tags(custom[r]))
        if r in removals:
            parts.append("remove " + ", ".join(f"{k}={r.tags[k]}" for k in removals[r]))
        if parts:
            extra += "  [" + "; ".join(parts) + "]"
        if r in reasons:
            extra += dim(f"  ({reasons[r]})")
        print(f"  {r.region:<12} {r.rtype:<27} {r.rid[:40]:<40} {r.name[:30]}{extra}")
    tally = Counter(decisions.values())
    print(dim(f"Skipped: {tally['skipped']}   Not reviewed: {tally['not reviewed']}   "
              f"Excluded: {len(excluded)}   Left out by --skip-tagged: {len(already_has)}   "
              + (f"Left out by --only: {len(not_matching)}   " if args.only else "")
              + (f"Left out by --only-tagged: {len(not_tagged_as)}   " if args.only_tagged else "") +
              f"Already tagged: {already}"))

    results = {}
    if args.dry_run:
        print(yellow("\nDry run: nothing was changed."))
    else:
        ans = (ask("\nType 'yes' to apply these tags (anything else cancels): ") or "").strip().lower()
        if ans == "yes":
            APPLYING = True
            print("Tagging ...", flush=True)
            results = apply_tags(session, selected, target, targets)
            if removals:
                print(f"Removing {sum(len(k) for k in removals.values())} differently-capitalized tag(s) ...",
                      flush=True)
                remove_variants(session, removals, results)
            APPLYING = False
            ok = sum(1 for v in results.values() if v.startswith("tagged"))
            print(green(f"Tagged {ok} of {len(selected)}."))
            removed = sum(1 for v in results.values() if "; removed" in v)
            if removals:
                print(green(f"Removed the extra copy from {removed} of {len(removals)}."))
            for r, v in results.items():
                if not v.startswith("tagged") or "couldn't" in v:
                    print(yellow(f"  {r.region} {r.rtype} {r.rid}: {v}"))
            if any(v != "tagged" and THROTTLE_RE.search(v) for v in results.values()):
                print("AWS was still throttling some requests. Wait a few minutes and run the same command "
                      "again; resources that are already tagged are skipped.")
        else:
            print("Cancelled. No changes made.")

    show_manual()
    write_log(log_path, resources, decisions, selected, results, target, targets, removals)
    print(f"Decision log: {log_path}")
    if results:
        print(dim("Tip: tags only show up in Cost Explorer after the keys are activated under "
                  "Billing and Cost Management > Cost allocation tags."))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        if APPLYING:
            print("\nInterrupted while tagging; some resources may already be tagged. Re-run to finish.")
        else:
            print("\nStopped. No tags were changed.")
        sys.exit(130)