# -*- encoding: utf-8 -*-
#
# Copyright © 2017 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

# NOTE(sileht): usefull for gunicon, not really for uwsgi
# import gevent
# import gevent.monkey
# gevent.monkey.patch_all()

import hmac
import json
import logging

import flask
import github
import raven.contrib.flask
import rq
import rq_dashboard
import uhashring

from mergify_engine import config
from mergify_engine import utils
from mergify_engine import worker


LOG = logging.getLogger(__name__)

app = flask.Flask(__name__)

app.config.from_object(rq_dashboard.default_settings)
app.register_blueprint(rq_dashboard.blueprint, url_prefix="/rq")
app.config["REDIS_URL"] = utils.get_redis_url()
app.config["RQ_POLL_INTERVAL"] = 10000  # ms
sentry = raven.contrib.flask.Sentry(app, dsn=config.SENTRY_URL)

# TODO(sileht): Make the ring dynamic
global RING
nodes = []
for fqdn, w in sorted(config.TOPOLOGY.items()):
    nodes.extend(map(lambda x: "%s-%003d" % (fqdn, x), range(w)))

RING = uhashring.HashRing(nodes=nodes)


def get_queue(slug, subscription):
    global RING
    name = "%s-%s" % (RING.get_node(slug),
                      "high" if subscription["subscribed"] else "low")
    return rq.Queue(name, connection=utils.get_redis_for_rq())


def authentification():  # pragma: no cover
    # Only SHA1 is supported
    header_signature = flask.request.headers.get('X-Hub-Signature')
    if header_signature is None:
        LOG.warning("Webhook without signature")
        flask.abort(403)

    try:
        sha_name, signature = header_signature.split('=')
    except ValueError:
        sha_name = None

    if sha_name != 'sha1':
        LOG.warning("Webhook signature malformed")
        flask.abort(403)

    mac = utils.compute_hmac(flask.request.data)
    if not hmac.compare_digest(mac, str(signature)):
        LOG.warning("Webhook signature invalid")
        flask.abort(403)


@app.route("/check_status_msg/<path:key>")
def check_status_msg(key):
    msg = utils.get_redis_for_cache().hget("status", key)
    if msg:
        return flask.render_template("msg.html", msg=msg)
    else:
        flask.abort(404)


@app.route("/refresh/<owner>/<repo>/<path:refresh_ref>",
           methods=["POST"])
def refresh(owner, repo, refresh_ref):
    authentification()

    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    installation_id = utils.get_installation_id(integration, owner)
    if not installation_id:  # pragma: no cover
        flask.abort(400, "%s have not installed mergify_engine" % owner)

    token = integration.get_access_token(installation_id).token
    g = github.Github(token)
    r = g.get_repo("%s/%s" % (owner, repo))
    try:
        r.get_contents(".mergify.yml")
    except github.GithubException as e:  # pragma: no cover
        if e.status == 404:
            return "No .mergify.yml", 202
        else:
            raise

    if refresh_ref == "full" or refresh_ref.startswith("branch/"):
        if refresh_ref.startswith("branch/"):
            branch = refresh_ref[7:]
            pulls = r.get_pulls(base=branch)
        else:
            branch = '*'
            pulls = r.get_pulls()
        key = "queues~%s~%s~%s~%s~%s" % (installation_id, owner.lower(),
                                         repo.lower(), r.private, branch)
        utils.get_redis_for_cache().delete(key)
    else:
        try:
            pull_number = int(refresh_ref[5:])
        except ValueError:  # pragma: no cover
            return "Invalid PR ref", 400
        pulls = [r.get_pull(pull_number)]

    subscription = utils.get_subscription(utils.get_redis_for_cache(),
                                          installation_id)

    if not subscription["token"]:  # pragma: no cover
        return "", 202

    if r.private and not subscription["subscribed"]:  # pragma: no cover
        return "", 202

    for p in pulls:
        # Mimic the github event format
        data = {
            'repository': r.raw_data,
            'installation': {'id': installation_id},
            'pull_request': p.raw_data,
        }
        get_queue(r.full_name, subscription).enqueue(
            worker.event_handler, "refresh", subscription, data)

    return "", 202


@app.route("/refresh", methods=["POST"])
def refresh_all():
    authentification()

    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)

    counts = [0, 0, 0]
    for install in utils.get_installations(integration):
        counts[0] += 1
        token = integration.get_access_token(install["id"]).token
        g = github.Github(token)
        i = g.get_installation(install["id"])

        subscription = utils.get_subscription(utils.get_redis_for_cache(),
                                              install["id"])
        if not subscription["token"]:  # pragma: no cover
            continue

        for r in i.get_repos():
            if r.private and not subscription["subscribed"]:
                continue
            try:
                r.get_contents(".mergify.yml")
            except github.GithubException as e:  # pragma: no cover
                if e.status == 404:
                    continue
                else:
                    raise

            counts[1] += 1
            for p in list(r.get_pulls()):
                # Mimic the github event format
                data = {
                    'repository': r.raw_data,
                    'installation': {'id': install["id"]},
                    'pull_request': p.raw_data,
                }
                get_queue(r.full_name, subscription).enqueue(
                    worker.event_handler, "refresh", subscription, data)

    return ("Updated %s installations, %s repositories, "
            "%s branches" % tuple(counts)), 202


# FIXME(sileht): rename this to new subscription something
@app.route("/subscription-cache/<installation_id>", methods=["DELETE"])
def subscription_cache(installation_id):  # pragma: no cover
    authentification()
    r = utils.get_redis_for_cache()
    r.delete("subscription-cache-%s" % installation_id)

    subscription = utils.get_subscription(
        utils.get_redis_for_cache(), installation_id)

    # New subscription, create initial configuration for private repo
    # public repository have already been done during the installation
    # event.
    if subscription["token"] and subscription["subscribed"]:
        # FIXME(sileht): We should pass the slugs
        get_queue(installation_id, subscription).enqueue(
            worker.installation_handler, installation_id, "private")
    return "Cache cleaned", 200


@app.route("/event", methods=["POST"])
def event_handler():
    authentification()

    event_type = flask.request.headers.get("X-GitHub-Event")
    event_id = flask.request.headers.get("X-GitHub-Delivery")
    data = flask.request.get_json()

    subscription = utils.get_subscription(
        utils.get_redis_for_cache(), data["installation"]["id"])

    if not subscription["token"]:
        msg_action = "ignored (no token)"

    elif event_type == "installation" and data["action"] == "created":
        for repository in data["repositories"]:
            if repository["private"] and not subscription["subscribed"]:  # noqa pragma: no cover
                continue

            get_queue(repository["full_name"], subscription).enqueue(
                worker.installation_handler, data["installation"]["id"],
                [repository])
        msg_action = "pushed to backend"

    elif event_type == "installation" and data["action"] == "deleted":
        key = "queues~%s~*~*~*~*" % data["installation"]["id"]
        utils.get_redis_for_cache().delete(key)
        msg_action = "handled, cache cleaned"

    elif (event_type == "installation_repositories" and
          data["action"] == "added"):
        for repository in data["repositories_added"]:
            if repository["private"] and not subscription["subscribed"]:  # noqa pragma: no cover
                continue

            get_queue(repository["full_name"], subscription).enqueue(
                worker.installation_handler, data["installation"]["id"],
                [repository])

        msg_action = "pushed to backend"

    elif (event_type == "installation_repositories" and
          data["action"] == "removed"):
        for repository in data["repositories_removed"]:
            if repository["private"] and not subscription["subscribed"]:  # noqa pragma: no cover
                continue
            key = "queues~%s~%s~%s~*~*" % (
                data["installation"]["id"],
                data["installation"]["account"]["login"].lower(),
                repository["name"].lower()
            )
            utils.get_redis_for_cache().delete(key)
        msg_action = "handled, cache cleaned"

    elif event_type in ["installation", "installation_repositories"]:
        msg_action = "ignored (action %s)" % data["action"]

    elif event_type in ["pull_request", "pull_request_review", "status",
                        "refresh"]:

        if data["repository"]["private"] and not subscription["subscribed"]:
            msg_action = "ignored (not public or subscribe)"

        elif event_type == "status" and data["state"] == "pending":
            msg_action = "ignored (state pending)"

        elif (event_type == "pull_request" and data["action"] not in [
                "opened", "reopened", "closed", "synchronize",
                "labeled", "unlabeled"]):
            msg_action = "ignored (action %s)" % data["action"]

        else:
            get_queue(data["repository"]["full_name"], subscription).enqueue(
                worker.event_handler, event_type, subscription, data)
            msg_action = "pushed to backend"

    else:
        msg_action = "ignored (unexpected event_type)"

    if "repository" in data:
        repo_name = data["repository"]["full_name"]
    else:
        repo_name = data["installation"]["account"]["login"]

    LOG.info('[%s/%s] received "%s" event "%s", %s',
             data["installation"]["id"], repo_name,
             event_type, event_id, msg_action)

    return "", 202


# NOTE(sileht): These endpoints are used for recording cassetes, we receive
# Github event on POST, we store them is redis, GET to retreive and delete
@app.route("/events-testing", methods=["POST", "GET", "DELETE"])
def event_testing_handler():  # pragma: no cover
    authentification()
    r = utils.get_redis_for_cache()
    if flask.request.method == "DELETE":
        r.delete("events-testing")
        return "", 202
    elif flask.request.method == "POST":
        event_type = flask.request.headers.get("X-GitHub-Event")
        event_id = flask.request.headers.get("X-GitHub-Delivery")
        data = flask.request.get_json()
        r.rpush("events-testing", json.dumps(
            {"id": event_id, "type": event_type, "payload": data}
        ))
        return "", 202
    else:
        p = r.pipeline()
        p.lrange("events-testing", 0, -1)
        p.delete("events-testing")
        values = p.execute()[0]
        data = [json.loads(i) for i in values]
        return flask.jsonify(data)


@app.route("/")
def index():  # pragma: no cover
    return flask.redirect("https://mergify.io/")
