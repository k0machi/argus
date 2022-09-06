from datetime import datetime
import json
import logging
import re
import time
from typing import Any
from uuid import UUID

import requests
from flask import g

from argus.backend.models.web import (
    ArgusEvent,
    ArgusEventTypes,
    ArgusGithubIssue,
    ArgusGroup,
    ArgusNotificationSourceTypes,
    ArgusNotificationTypes,
    ArgusRelease,
    ArgusTest,
    ArgusTestRunComment,
    User,
    UserOauthToken,
)

from argus.backend.plugins.core import PluginInfoBase, PluginModelBase

from argus.backend.plugins.loader import AVAILABLE_PLUGINS
from argus.backend.events.event_processors import EVENT_PROCESSORS
from argus.backend.service.notification_manager import NotificationManagerService
from argus.backend.util.common import get_build_number, strip_html_tags
from argus.backend.util.enums import TestInvestigationStatus, TestStatus

LOGGER = logging.getLogger(__name__)


class TestRunService:

    RE_MENTION = r"@[A-Za-z\d](?:[A-Za-z\d]|-(?=[A-Za-z\d])){0,38}"

    plugins = AVAILABLE_PLUGINS
    github_headers = {
        "Accept": "application/vnd.github.v3+json",
    }

    def __init__(self) -> None:
        self.notification_manager = NotificationManagerService()

    def get_plugin(self, plugin_name: str) -> PluginInfoBase | None:
        return self.plugins.get(plugin_name)

    def get_run(self, run_type: str, run_id: UUID) -> PluginModelBase:
        plugin = self.plugins.get(run_type)
        if plugin:
            try:
                return plugin.model.get(id=run_id)
            except plugin.model.DoesNotExist:
                return None

    def get_runs_by_test_id(self, test_id: UUID, additional_runs: list[UUID], limit: int = 10):
        test: ArgusTest = ArgusTest.get(id=test_id)
        plugin = self.get_plugin(plugin_name=test.plugin_name)
        if not plugin:
            return []

        last_runs: list[PluginModelBase] = list(plugin.model.filter(build_id=test.build_system_id).limit(limit).all())
        last_runs_ids = [run.id for run in last_runs]
        for added_run in additional_runs:
            if added_run not in last_runs_ids:
                last_runs.append(plugin.model.get(id=added_run))

        for row in last_runs:
            setattr(row, "build_number", get_build_number(build_job_url=row.build_job_url))

        return last_runs

    def get_runs_by_id(self, test_id: UUID, runs: list[UUID]):  # FIXME: Not needed, use get_run and individual polling
        # This is a batch request.
        test = ArgusTest.get(id=test_id)
        plugin = self.get_plugin(plugin_name=test.plugin_name)
        polled_runs: list[PluginModelBase] = []
        for run_id in runs:
            try:
                run: PluginModelBase = plugin.model.get(id=run_id)
                polled_runs.append(run)
            except plugin.model.DoesNotExist:
                pass

        response = {str(run.id): run for run in polled_runs}
        return response

    def change_run_status(self, test_id: UUID, run_id: UUID, new_status: TestStatus):
        test = ArgusTest.get(id=test_id)
        plugin = self.get_plugin(plugin_name=test.plugin_name)
        run: PluginModelBase = plugin.model.get(id=run_id)
        old_status = run.status
        run.status = new_status.value
        run.save()

        self.create_run_event(
            kind=ArgusEventTypes.TestRunStatusChanged,
            body={
                "message": "Status was changed from {old_status} to {new_status} by {username}",
                "old_status": old_status,
                "new_status": new_status.value,
                "username": g.user.username
            },
            user_id=g.user.id,
            run_id=run.id,
            release_id=test.release_id,
            group_id=test.group_id,
            test_id=test.id
        )

        return {
            "test_run_id": run.id,
            "status": new_status
        }

    def change_run_investigation_status(self, test_id: UUID, run_id: UUID, new_status: TestInvestigationStatus):
        test = ArgusTest.get(id=test_id)
        plugin = self.get_plugin(plugin_name=test.plugin_name)
        run: PluginModelBase = plugin.model.get(id=run_id)
        old_status = run.investigation_status
        run.investigation_status = new_status.value
        run.save()

        self.create_run_event(
            kind=ArgusEventTypes.TestRunStatusChanged,
            body={
                "message": "Investigation status was changed from {old_status} to {new_status} by {username}",
                "old_status": old_status,
                "new_status": new_status.value,
                "username": g.user.username
            },
            user_id=g.user.id,
            run_id=run.id,
            release_id=test.release_id,
            group_id=test.group_id,
            test_id=test.id
        )

        return {
            "test_run_id": run.id,
            "investigation_status": new_status
        }

    def change_run_assignee(self, test_id: UUID, run_id: UUID, new_assignee: UUID):
        test = ArgusTest.get(id=test_id)
        plugin = self.get_plugin(plugin_name=test.plugin_name)
        run: PluginModelBase = plugin.model.get(id=run_id)
        old_assignee = run.assignee
        run.assignee = new_assignee
        run.save()

        new_assignee_user = User.get(id=new_assignee)
        if old_assignee:
            old_assignee_user = User.get(id=old_assignee)
        self.create_run_event(
            kind=ArgusEventTypes.AssigneeChanged,
            body={
                "message": "Assignee was changed from \"{old_user}\" to \"{new_user}\" by {username}",
                "old_user": old_assignee_user.username if old_assignee else "None",
                "new_user": new_assignee_user.username,
                "username": g.user.username
            },
            user_id=g.user.id,
            run_id=run.id,
            release_id=test.release_id,
            group_id=test.group_id,
            test_id=test.id
        )
        return {
            "test_run_id": run.id,
            "assignee": str(new_assignee_user.id)
        }

    def get_run_comment(self, comment_id: UUID):
        try:
            return ArgusTestRunComment.get(id=comment_id)
        except ArgusTestRunComment.DoesNotExist:
            return None

    def get_run_comments(self, run_id: UUID):
        return sorted(ArgusTestRunComment.filter(test_run_id=run_id).all(), key=lambda c: c.posted_at)

    def post_run_comment(self, test_id: UUID, run_id: UUID, message: str, reactions: dict, mentions: list[str]):
        message_stripped = strip_html_tags(message)

        mentions = set(mentions)
        for potential_mention in re.findall(self.RE_MENTION, message_stripped):
            if user := User.exists_by_name(potential_mention.lstrip("@")):
                mentions.add(user)

        test: ArgusTest = ArgusTest.get(id=test_id)
        plugin = self.get_plugin(test.plugin_name)
        release: ArgusRelease = ArgusRelease.get(id=test.release_id)
        comment = ArgusTestRunComment()
        comment.message = message_stripped
        comment.reactions = reactions
        comment.mentions = [m.id for m in mentions]
        comment.test_run_id = run_id
        comment.release_id = release.id
        comment.user_id = g.user.id
        comment.posted_at = time.time()
        comment.save()

        run: PluginModelBase = plugin.model.get(id=run_id)
        build_number = get_build_number(build_job_url=run.build_job_url)
        for mention in mentions:
            params = {
                "username": g.user.username,
                "run_id": comment.test_run_id,
                "test_id": test.id,
                "build_id": run.build_id,
                "build_number": build_number,
            }
            self.notification_manager.send_notification(
                receiver=mention.id,
                sender=comment.user_id,
                notification_type=ArgusNotificationTypes.Mention,
                source_type=ArgusNotificationSourceTypes.Comment,
                source_id=comment.id,
                content_params=params
            )

        self.create_run_event(kind=ArgusEventTypes.TestRunCommentPosted, body={
            "message": "A comment was posted by {username}",
            "username": g.user.username
        }, user_id=g.user.id, run_id=run_id, release_id=release.id, test_id=test.id)

        return self.get_run_comments(run_id=run_id)

    def delete_run_comment(self, comment_id: UUID, test_id: UUID, run_id: UUID):
        comment: ArgusTestRunComment = ArgusTestRunComment.get(id=comment_id)
        if comment.user_id != g.user.id:
            raise Exception("Unable to delete other user comments")
        comment.delete()

        self.create_run_event(kind=ArgusEventTypes.TestRunCommentDeleted, body={
            "message": "A comment was deleted by {username}",
            "username": g.user.username
        }, user_id=g.user.id, run_id=UUID(run_id), release_id=comment.release_id, test_id=test_id)

        return self.get_run_comments(run_id=run_id)

    def update_run_comment(self, comment_id: UUID, test_id: UUID, run_id: UUID, message: str, mentions: list[str], reactions: dict):
        comment: ArgusTestRunComment = ArgusTestRunComment.get(id=comment_id)
        if comment.user_id != g.user.id:
            raise Exception("Unable to edit other user comments")
        comment.message = strip_html_tags(message)
        comment.reactions = reactions
        comment.mentions = mentions
        comment.save()

        self.create_run_event(kind=ArgusEventTypes.TestRunCommentUpdated, body={
            "message": "A comment was edited by {username}",
            "username": g.user.username
        }, user_id=g.user.id, run_id=run_id, release_id=comment.release_id, test_id=test_id)

        return self.get_run_comments(run_id=run_id)

    def create_run_event(self, kind: ArgusEventTypes, body: dict, user_id=None, run_id=None, release_id=None, group_id=None, test_id=None):
        event = ArgusEvent()
        event.release_id = release_id
        event.group_id = group_id
        event.test_id = test_id
        event.user_id = user_id
        event.run_id = run_id
        event.body = json.dumps(body, ensure_ascii=True, separators=(',', ':'))
        event.kind = kind.value
        event.created_at = datetime.utcnow()
        event.save()

    def get_run_events(self, run_id: UUID):
        response = {}
        all_events = ArgusEvent.filter(run_id=run_id).all()
        all_events = sorted(all_events, key=lambda ev: ev.created_at)
        response["run_id"] = run_id
        response["raw_events"] = [dict(event.items()) for event in all_events]
        response["events"] = {
            str(event.id): EVENT_PROCESSORS.get(event.kind)(json.loads(event.body))
            for event in all_events
        }
        return response

    def submit_github_issue(self, issue_url: str, test_id: UUID, run_id: UUID):
        user_tokens = UserOauthToken.filter(user_id=g.user.id).all()
        token = None
        for tok in user_tokens:
            if tok.kind == "github":
                token = tok.token
                break
        if not token:
            raise Exception("Github token not found")

        match = re.match(
            r"http(s)?://(www\.)?github\.com/(?P<owner>[\w\d]+)/"
            r"(?P<repo>[\w\d\-_]+)/(?P<type>issues|pull)/(?P<issue_number>\d+)(/)?",
            issue_url,
        )
        if not match:
            raise Exception("URL doesn't match Github schema")

        test: ArgusTest = ArgusTest.get(id=test_id)
        plugin = self.get_plugin(plugin_name=test.plugin_name)

        run = plugin.model.get(id=run_id)
        release = ArgusRelease.get(id=run["release_id"])
        test = ArgusTest.get(build_system_id=run["build_id"])
        group = ArgusGroup.get(id=test.group_id)

        new_issue = ArgusGithubIssue()
        new_issue.user_id = g.user.id
        new_issue.run_id = run_id
        new_issue.group_id = group.id
        new_issue.release_id = release.id
        new_issue.test_id = test.id
        new_issue.type = match.group("type")
        new_issue.owner = match.group("owner")
        new_issue.repo = match.group("repo")
        new_issue.issue_number = int(match.group("issue_number"))

        issue_request = requests.get(
            f"https://api.github.com/repos/{new_issue.owner}/{new_issue.repo}/issues/{new_issue.issue_number}",
            headers={
                **self.github_headers,
                "Authorization": f"token {token}",
            }
        )
        if issue_request.status_code != 200:
            raise Exception(f"Error getting issue state: Response: HTTP {issue_request.status_code}", issue_request)

        issue_state: dict[str, Any] = issue_request.json()

        new_issue.title = issue_state.get("title")
        new_issue.url = issue_state.get("html_url")
        new_issue.last_status = issue_state.get("state")
        new_issue.save()

        self.create_run_event(
            kind=ArgusEventTypes.TestRunIssueAdded,
            body={
                "message": "An issue titled \"{title}\" was added by {username}",
                "username": g.user.username,
                "url": issue_url,
                "title": issue_state.get("title"),
                "state": issue_state.get("state"),
            },
            user_id=g.user.id,
            run_id=new_issue.run_id,
            release_id=new_issue.release_id,
            group_id=new_issue.group_id,
            test_id=new_issue.test_id
        )

        response = {
            **dict(list(new_issue.items())),
            "title": issue_state.get("title"),
            "state": issue_state.get("state"),
        }

        return response

    def get_github_issues(self, filter_key: str, filter_id: UUID, aggregate_by_issue: bool = False) -> dict:
        if filter_key not in ["release_id", "group_id", "test_id", "run_id", "user_id"]:
            raise Exception(
                "filter_key can only be one of: \"release_id\", \"group_id\", \"test_id\", \"run_id\", \"user_id\""
            )

        all_issues = ArgusGithubIssue.filter(**{filter_key: filter_id}).all()
        if aggregate_by_issue:
            runs_by_issue = {}
            response = []
            for issue in all_issues:
                runs = runs_by_issue.get(issue, [])
                runs.append(issue.run_id)
                runs_by_issue[issue] = runs

            for issue, runs in runs_by_issue.items():
                issue_dict = dict(issue.items())
                issue_dict["runs"] = runs
                response.append(issue_dict)

        else:
            response = [dict(issue.items()) for issue in all_issues]
        return response

    def delete_github_issue(self, issue_id: UUID) -> dict:
        issue: ArgusGithubIssue = ArgusGithubIssue.get(id=issue_id)

        self.create_run_event(
            kind=ArgusEventTypes.TestRunIssueRemoved,
            body={
                "message": "An issue titled \"{title}\" was removed by {username}",
                "username": g.user.username,
                "url": issue.url,
                "title": issue.title,
                "state": issue.last_status,
            },
            user_id=g.user.id,
            run_id=issue.run_id,
            release_id=issue.release_id,
            group_id=issue.group_id,
            test_id=issue.test_id
        )
        issue.delete()

        return {
            "deleted": issue_id
        }