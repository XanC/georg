# SPDX-License-Identifier: AGPL-3.0-only
"""Offline structure checks for deploy/aws/georg.yaml (cfn-lint runs in CI too)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "deploy" / "aws" / "georg.yaml"
WAKE = ROOT / "deploy" / "aws" / "wake" / "index.py"


class _CfnLoader(yaml.SafeLoader):
    pass


def _tag(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> dict[str, Any]:
    name = "Ref" if suffix == "Ref" else f"Fn::{suffix}"
    value: Any
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        assert isinstance(node, yaml.MappingNode)
        value = loader.construct_mapping(node, deep=True)
    return {name: value}


_CfnLoader.add_multi_constructor("!", _tag)


@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    loaded = yaml.load(TEMPLATE.read_text(), Loader=_CfnLoader)
    assert isinstance(loaded, dict)
    return loaded


def _resources(template: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [r for r in template["Resources"].values() if r["Type"] == kind]


def _one(template: dict[str, Any], kind: str) -> dict[str, Any]:
    found = _resources(template, kind)
    assert len(found) == 1, kind
    props: dict[str, Any] = found[0]["Properties"]
    return props


def test_parameter_defaults(template: dict[str, Any]) -> None:
    params = template["Parameters"]
    assert params["InstanceType"]["Default"] == "t3.large"
    assert params["DataVolumeSize"]["Default"] == 50
    assert params["Hostname"]["Default"] == "georg.stpaulaustin.org"
    assert params["OriginHostname"]["Default"] == "origin.georg.stpaulaustin.org"
    assert params["HostedZoneId"]["Default"] == ""
    assert params["CertificateArn"]["Default"] == ""
    assert params["WakeTimezone"]["Default"] == "America/Chicago"
    assert "SUN" in params["WakeSchedule"]["Default"]


def test_expected_resources(template: dict[str, Any]) -> None:
    kinds = {r["Type"] for r in template["Resources"].values()}
    assert {
        "AWS::EC2::Instance",
        "AWS::EC2::Volume",
        "AWS::EC2::VolumeAttachment",
        "AWS::IAM::Role",
        "AWS::S3::Bucket",
        "AWS::Scheduler::Schedule",
        "AWS::Lambda::Function",
        "AWS::Lambda::Url",
        "AWS::DLM::LifecyclePolicy",
    } <= kinds
    assert not _resources(template, "AWS::EC2::EIP")


def test_instance_stops_on_poweroff_and_requires_imdsv2(template: dict[str, Any]) -> None:
    assert _one(template, "AWS::EC2::Instance")["InstanceInitiatedShutdownBehavior"] == "stop"
    data = _one(template, "AWS::EC2::LaunchTemplate")["LaunchTemplateData"]
    assert data["MetadataOptions"]["HttpTokens"] == "required"


def test_data_volume(template: dict[str, Any]) -> None:
    volume = template["Resources"]["DataVolume"]
    assert volume["Properties"]["VolumeType"] == "gp3"
    assert volume["Properties"]["Encrypted"] is True
    assert volume["DeletionPolicy"] == "Snapshot"
    tags = {t["Key"]: t["Value"] for t in volume["Properties"]["Tags"]}
    target = _one(template, "AWS::DLM::LifecyclePolicy")["PolicyDetails"]["TargetTags"]
    assert {t["Key"]: t["Value"] for t in target}.items() <= tags.items()


def test_backup_bucket_private_versioned_encrypted(template: dict[str, Any]) -> None:
    bucket = template["Resources"]["BackupBucket"]
    props = bucket["Properties"]
    assert bucket["DeletionPolicy"] == "Retain"
    assert props["VersioningConfiguration"]["Status"] == "Enabled"
    assert all(props["PublicAccessBlockConfiguration"].values())
    assert props["BucketEncryption"]["ServerSideEncryptionConfiguration"]


def test_instance_role_permissions(template: dict[str, Any]) -> None:
    role = template["Resources"]["InstanceRole"]["Properties"]
    statements = role["Policies"][0]["PolicyDocument"]["Statement"]
    actions = {a for s in statements if "Action" in s for a in s["Action"]} | {
        s["Action"] for s in statements if isinstance(s.get("Action"), str)
    }
    assert {"ssm:GetParametersByPath", "s3:PutObject", "ses:SendRawEmail"} <= actions
    (dns,) = [s for s in statements if "Fn::If" in s]
    condition, allowed, otherwise = dns["Fn::If"]
    assert condition == "HasDns"
    assert allowed["Action"] == "route53:ChangeResourceRecordSets"
    assert otherwise == {"Ref": "AWS::NoValue"}
    for s in statements:
        if "Action" in s:
            assert s["Resource"] != "*", s


def test_wake_schedule(template: dict[str, Any]) -> None:
    schedule = _one(template, "AWS::Scheduler::Schedule")
    assert schedule["ScheduleExpressionTimezone"] == {"Ref": "WakeTimezone"}
    assert schedule["Target"]["Arn"].endswith("ec2:startInstances")


def test_inline_wake_code_matches_source(template: dict[str, Any]) -> None:
    code = template["Resources"]["WakeFunction"]["Properties"]["Code"]["ZipFile"]
    assert code == WAKE.read_text()
    assert len(code) <= 4096, "CloudFormation limits inline Lambda code to 4096 characters"


def test_cloudfront_distribution(template: dict[str, Any]) -> None:
    distribution = template["Resources"]["Distribution"]
    assert distribution["Condition"] == "HasCertificate"
    config = distribution["Properties"]["DistributionConfig"]
    assert config["Aliases"] == [{"Ref": "Hostname"}]
    assert config["ViewerCertificate"]["AcmCertificateArn"] == {"Ref": "CertificateArn"}
    origins = {o["Id"]: o for o in config["Origins"]}
    assert origins["instance"]["DomainName"] == {"Ref": "OriginHostname"}
    assert origins["instance"]["CustomOriginConfig"]["OriginProtocolPolicy"] == "https-only"
    wake_domain = origins["wake"]["DomainName"]
    assert wake_domain["Fn::Select"][0] == 2
    assert wake_domain["Fn::Select"][1]["Fn::Split"][1] == {"Fn::GetAtt": "WakeUrl.FunctionUrl"}
    (group,) = config["OriginGroups"]["Items"]
    members = [m["OriginId"] for m in group["Members"]["Items"]]
    assert members == ["instance", "wake"]
    codes = group["FailoverCriteria"]["StatusCodes"]["Items"]
    assert {503, 504} <= set(codes)
    behavior = config["DefaultCacheBehavior"]
    assert behavior["TargetOriginId"] == group["Id"]
    assert "POST" in behavior["AllowedMethods"]
    # CachingDisabled; AllViewerExceptHostHeader.
    assert behavior["CachePolicyId"] == "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
    assert behavior["OriginRequestPolicyId"] == "b689b0a8-53d0-40ab-baf2-68738e2966ac"
    assert template["Outputs"]["DistributionDomainName"]["Condition"] == "HasCertificate"


def test_user_data_files_exist(template: dict[str, Any]) -> None:
    script = template["Resources"]["Instance"]["Properties"]["UserData"]["Fn::Base64"]["Fn::Sub"]
    for rel in ("deploy/aws/boot/setup-data-volume.sh", "deploy/compose.yaml"):
        assert f"/opt/georg/{rel}" in script
        assert (ROOT / rel).exists()
    units = {p.name for p in (ROOT / "deploy/aws/systemd").iterdir()}
    enabled = ("georg-boot.service", "georg.service", "georg-poweroff.path")
    for unit in (*enabled, "georg-discover.timer"):
        assert unit in units
        assert unit in script
