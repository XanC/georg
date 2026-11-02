# SPDX-License-Identifier: AGPL-3.0-only
"""Wake origin (Lambda function URL): start the Georg instance, then keep reloading.

CloudFront fails over here when the instance origin does not answer. This page
never redirects: it starts the instance if needed and reloads the same URL, so a
later reload reaches the primary origin once Georg is healthy. The health check
uses ORIGIN_URL (the origin hostname, which resolves only while the instance is
running). Inlined into deploy/aws/georg.yaml; keep under 4096 chars.
"""

import html
import os
import urllib.request

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="5"><title>Georg</title></head>
<body style="font-family:sans-serif;max-width:32em;margin:4em auto;padding:0 1em">
<h1>Georg</h1><p>{}</p><p><small>This page reloads by itself.</small></p></body></html>"""
STARTING = "Starting Georg; this takes about a minute."


def healthy(url, timeout=3.0):
    try:
        with urllib.request.urlopen(url + "/healthz", timeout=timeout) as r:
            return r.status == 200
    except OSError:
        return False


def page(message, status=200):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"},
        "body": PAGE.format(html.escape(message)),
    }


def handler(event, context, ec2=None, probe=healthy):
    instance_id = os.environ["INSTANCE_ID"]
    origin_url = os.environ["ORIGIN_URL"].rstrip("/")
    if ec2 is None:
        import boto3

        ec2 = boto3.client("ec2")
    reservations = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"]
    state = reservations[0]["Instances"][0]["State"]["Name"]
    if state == "stopped":
        ec2.start_instances(InstanceIds=[instance_id])
        return page(STARTING)
    if state == "pending":
        return page(STARTING)
    if state == "running":
        if probe(origin_url):
            return page("Georg is up; reloading to hand you over to it.")
        return page(STARTING)
    if state == "stopping":
        return page("Georg is shutting down after being idle; it will start again shortly.")
    return page(f"Georg cannot be started (instance state: {state}).", 503)
