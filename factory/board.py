"""GitHub Projects v2 board mirror of the ticket queue.

The sqlite queue stays the source of truth; GitHub issues and the project
board are a mirror so a human can watch tickets move without a terminal.
One rule from `sync` carries over: record only what GitHub accepted, never
what we hoped it would accept — a failed call leaves local state untouched
and the next run retries.
"""

import json
import re
import subprocess
import urllib.error
import urllib.request

COLUMN_FOR = {
    "queued": "Open",
    "running": "In progress",
    "pr_open": "Review",
    "needs_review": "Review",
    "escalated": "Blocked",
    "merged": "Done",
    "closed": "Done",
}

CLOSED_STATUSES = ("merged", "closed")


class BoardError(Exception):
    pass


def _repo(cfg):
    slug = cfg.get("repo_slug") or cfg.get("repo", "")
    if re.fullmatch(r"[^/\\\s:]+/[^/\\\s:]+", slug.strip()):
        return slug.strip().strip("/")
    url = subprocess.run(
        "git -C \"{}\" remote get-url {}".format(cfg["repo"], cfg.get("remote", "origin")),
        shell=True, capture_output=True, text=True).stdout.strip()
    m = re.search(r"[^/]+/[^/]+(?:\.git)?$", url)
    if not m:
        raise BoardError(f"cannot derive owner/name from remote url: {url!r}")
    return m.group(0)[:-4] if url.endswith(".git") else m.group(0)


def _token(cfg):
    tok = cfg.get("gh_token")
    if tok:
        return tok.strip()
    out = subprocess.run(
        "printf \"protocol=https\\nhost=github.com\\n\\n\" | git credential fill",
        shell=True, capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    raise BoardError("no GitHub token: set gh_token or make git credential work")


def _request(cfg, url, method, body=None, graphql=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        "https://api.github.com/graphql" if graphql
        else "https://api.github.com" + url,
        data=data, method=method,
        headers={"Authorization": "token " + _token(cfg),
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read())
    except urllib.error.HTTPError as exc:
        raise BoardError(f"GitHub {method} failed ({exc.code}): "
                         f"{exc.read().decode()[:200]}") from exc


def gql(cfg, query, variables=None):
    r = _request(cfg, "", "POST", {"query": query, "variables": variables or {}},
                 graphql=True)
    if "errors" in r:
        raise BoardError("GraphQL: " + r["errors"][0].get("message", "?")[:200])
    return r["data"]


def ensure_columns(cfg, project_id):
    """Resolve the Status single-select options, creating any we need.

    The API has no "add one option" mutation: updateProjectV2Field replaces
    the whole option list, so we echo the existing options back (ids intact)
    and append the missing ones.
    """
    want = ["Open", "In progress", "Review", "Blocked", "Done"]
    colors = {"Open": "GRAY", "In progress": "YELLOW", "Review": "PURPLE",
              "Blocked": "RED", "Done": "GREEN"}
    d = gql(cfg, """query($id: ID!) { node(id: $id) { ... on ProjectV2 {
        fields(first: 30) { nodes {
          ... on ProjectV2SingleSelectField { id name options {
            id name description color } }
        } } } } }""", {"id": project_id})
    field = next((f for f in d["node"]["fields"]["nodes"]
                  if f.get("name") == "Status"), None)
    if field is None:
        raise BoardError("project has no Status field")
    options = {o["name"]: o["id"] for o in field.get("options", [])}
    missing = [w for w in want if w not in options]
    if missing:
        payload = [{"id": o["id"], "name": o["name"],
                    "description": o.get("description") or o["name"],
                    "color": o["color"]} for o in field["options"]]
        payload += [{"name": n, "description": n, "color": colors[n]}
                    for n in missing]
        gql(cfg, """mutation($fieldId: ID!, $options: [ProjectV2SingleSelectFieldOptionInput!]!) {
            updateProjectV2Field(input: {fieldId: $fieldId, singleSelectOptions: $options}) {
              projectV2Field { ... on ProjectV2SingleSelectField { id } } } }""",
            {"fieldId": field["id"], "options": payload})
        return ensure_columns(cfg, project_id)
    return field["id"], options


def ensure_issue(cfg, con, ticket):
    """Create the GitHub issue once; number and node id ride on the row."""
    if ticket["issue_number"]:
        return ticket["issue_number"]
    body = (f"Factory ticket {ticket['id']} (tier {ticket['tier'] or 'auto'}, "
            f"budget ${float(ticket['budget_usd']):.2f}).\n\n"
            + (ticket["body"] or ""))
    r = _request(cfg, f"/repos/{_repo(cfg)}/issues",
                 "POST", {"title": ticket["title"], "body": body})
    con.execute("UPDATE tickets SET issue_number=?, issue_node=?, issue_state='open' "
                "WHERE id=?",
                (r["number"], r["node_id"], ticket["id"]))
    con.commit()
    return r["number"]


def ensure_item(cfg, con, ticket, project_id):
    """Add the issue to the board once; the project item id rides on the row."""
    if ticket["project_item"]:
        return ticket["project_item"]
    ensure_issue(cfg, con, ticket)
    row = con.execute("SELECT issue_node FROM tickets WHERE id=?",
                      (ticket["id"],)).fetchone()
    d = gql(cfg, """mutation($projectId: ID!, $contentId: ID!) {
        addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
          item { id } } }""", {"projectId": project_id, "contentId": row["issue_node"]})
    item = d["addProjectV2ItemById"]["item"]["id"]
    con.execute("UPDATE tickets SET project_item=? WHERE id=?",
                (item, ticket["id"]))
    con.commit()
    return item


def set_column(cfg, ticket, project_id, field_id, option_id):
    gql(cfg, """mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!,
                         $value: String!) {
        updateProjectV2ItemFieldValue(input: {projectId: $projectId, itemId: $itemId,
          fieldId: $fieldId, value: {singleSelectOptionId: $value}}) { projectV2Item { id } } }""",
        {"projectId": project_id, "itemId": ticket["project_item"],
         "fieldId": field_id, "value": option_id})


def close_issue(cfg, con, ticket):
    if ticket["issue_state"] == "closed" or not ticket["issue_number"]:
        return
    _request(cfg, f"/repos/{_repo(cfg)}"
                  f"/issues/{ticket['issue_number']}", "PATCH", {"state": "closed"})
    con.execute("UPDATE tickets SET issue_state='closed' WHERE id=?", (ticket["id"],))
    con.commit()


def sync_board(cfg, con, dry=False):
    """Mirror every ticket's queue status onto the board. Idempotent."""
    project_id = cfg.get("project_id")
    if not project_id:
        raise BoardError("config needs project_id (Projects v2 node id)")
    field_id, options = None, {}
    if not dry:
        field_id, options = ensure_columns(cfg, project_id)
    moves = []
    for ticket in con.execute("SELECT * FROM tickets ORDER BY id").fetchall():
        column = COLUMN_FOR.get(ticket["status"])
        if column is None:
            continue
        if dry:
            moves.append((ticket["id"], ticket["status"], column))
            continue
        item = ensure_item(cfg, con, ticket, project_id)
        set_column(cfg, {"project_item": item}, project_id, field_id, options[column])
        if ticket["status"] in CLOSED_STATUSES:
            close_issue(cfg, con, ticket)
        moves.append((ticket["id"], ticket["status"], column))
    return moves
