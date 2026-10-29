"""Who may do what: the roles, the actions each may take, and who may message whom and post where.

The roles:
- the super admin: whoever holds the server's admin token, the operator who sets the rules. It is not a user and
  never posts, and only it sets the rules.
- administrators: users the rules name.
- channel creators: users the rules name. Without that list, every user is one.
- a channel's managers: its creator, and anyone a manager or administrator names. They hold the role in that channel
  only.
- members: every user.

Rules come from DIR/policy.json, set with `swarmchat admin policy-set FILE`:

    {"admins": ["dana"], "channel_creators": ["maya", "priya"],
     "contacts": {"omar": ["maya", "priya"], "maya": ["ken"]}, "symmetric": true,
     "posters": {"#records": ["verifier"]},
     "allow": {"edit_admins": ["superadmin", "admin"]}}

Each action in ACTIONS lists the roles that may take it. "allow" replaces that list for any action, so the super
admin decides, for example, that administrators may name administrators, as above. Until the rules name
administrators the server is open: any member also does what a manager does, as in OPEN. Whatever the roles, nobody
acts in a chat they are not in, so private channels and direct chats stay private from administrators too.
`swarmchat admin role` changes admins and channel_creators for whoever "edit_admins" or "edit_channel_creators"
allows.

With contacts set, a user may start or write in a direct chat only if every other member is one of their contacts,
and may create a channel with people or add people to one only if those people are their contacts. Anyone may join a
public channel: making it public was the check. Users missing from the map may contact no one. "symmetric" (the
default) makes each contact work both ways.

Posters names channels, by name or id, where only the listed users may post or edit. Other members still read and
react. Anyone in any other channel may post in it: being added was the check. A channel named here by its name cannot
be renamed, so the rule cannot be dropped that way.

History says what a channel's members may read. "all" (the default, as in Slack) is everything, from before they
joined too. "while_member" is only what was sent while they were in it: not before they joined, and not while they
were out of it. It holds for reads, searches, threads, files, previews and counts. Direct chats keep their members,
so they are not affected.

For other rules, edit this class: each check returns None to allow, or the reason it refuses.
"""
from __future__ import annotations

SUPER, ADMIN, CREATOR, MANAGER, MEMBER = "superadmin", "admin", "channel_creator", "manager", "member"
ROLES = {SUPER: "the super admin", ADMIN: "an administrator", CREATOR: "someone chosen to create channels",
         MANAGER: "a channel manager", MEMBER: "a member"}

# action: (what it is, the roles that may take it). The last nine are taken in a chat, by its members.
ACTIONS = {
    "add_user": ("add users", [SUPER]),
    "remove_user": ("remove users", [SUPER]),
    "rotate_token": ("give someone else a new token", [SUPER]),
    "show_rules": ("see the rules", [SUPER, ADMIN]),
    "edit_admins": ("choose the administrators", [SUPER]),
    "edit_channel_creators": ("choose who may create channels", [SUPER, ADMIN]),
    "create_channel": ("create a channel", [CREATOR, ADMIN]),
    "add_members": ("add people to a channel", [MEMBER]),
    "remove_members": ("remove someone else from a channel", [MANAGER, ADMIN]),
    "rename_channel": ("rename a channel", [MANAGER, ADMIN]),
    "describe_channel": ("change a channel's description", [MANAGER, ADMIN]),
    "make_private": ("make a channel private", [MANAGER, ADMIN]),
    "make_public": ("make a private channel public", [ADMIN]),  # its whole history opens to everyone, as in Slack
    "archive_channel": ("archive or unarchive a channel", [MANAGER, ADMIN]),
    "set_managers": ("choose a channel's managers", [MANAGER, ADMIN]),
    "delete_message": ("delete another person's message", [ADMIN]),
}
OPEN = {action: [MEMBER] for action in ("remove_members", "rename_channel", "describe_channel", "make_private",
                                        "make_public", "archive_channel", "set_managers")}


def _users(rules: dict, key: str) -> set | None:
    users = rules.get(key)
    if users is None:
        return None
    if isinstance(users, str) or not isinstance(users, list) or not all(isinstance(u, str) for u in users):
        raise ValueError(f"{key} is a list of user ids.")
    return {u.strip().lower() for u in users}


class Policy:
    def __init__(self, rules: dict | None = None):
        self.rules = rules or {}
        contacts = self.rules.get("contacts")
        self.contacts = None
        if contacts is not None:
            self.contacts = {user: set(others) for user, others in contacts.items()}
            if self.rules.get("symmetric", True):
                for user, others in list(self.contacts.items()):
                    for other in others:
                        self.contacts.setdefault(other, set()).add(user)
        posters = self.rules.get("posters")
        self.posters = None
        if posters is not None:
            self.posters = {chat.strip().lstrip("#").strip().lower(): set(users) for chat, users in posters.items()}
        self.admins, self.creators = _users(self.rules, "admins"), _users(self.rules, "channel_creators")
        self.history = self.rules.get("history", "all")
        if self.history not in ("all", "while_member"):
            raise ValueError('history is "all" or "while_member".')
        allow = self.rules.get("allow") or {}
        if not isinstance(allow, dict):
            raise ValueError("allow maps actions to lists of roles.")
        for action, roles in allow.items():
            if action not in ACTIONS:
                raise ValueError(f"There is no action '{action}'. The actions are {', '.join(ACTIONS)}.")
            if isinstance(roles, str) or not isinstance(roles, list) or not set(roles) <= set(ROLES):
                raise ValueError(f"allow.{action} is a list of roles: {', '.join(ROLES)}.")
        self.allow = {action: roles for action, (_, roles) in ACTIONS.items()}  # in the order written, for refusals
        if self.admins is None:
            self.allow.update(OPEN)
        self.allow.update((action, list(dict.fromkeys(roles))) for action, roles in allow.items())

    def admin(self, user: str) -> bool:
        """Whether `user` is an administrator."""
        return self.admins is not None and user in self.admins

    def roles(self, user: str | None, managers=None) -> set:
        """The roles of `user`, or of the super admin for None. `managers` are the managers of the chat acted in."""
        if user is None:
            return {SUPER}
        roles = {MEMBER}
        if self.admin(user):
            roles.add(ADMIN)
        if self.creators is None or user in self.creators:
            roles.add(CREATOR)
        if managers is not None and user in managers:
            roles.add(MANAGER)
        return roles

    def check(self, action: str, roles: set) -> str | None:
        allowed = self.allow[action]
        if roles.intersection(allowed):
            return None
        what = ACTIONS[action][0]
        if not allowed:
            return f"No one may {what}."
        return f"Only {' or '.join(ROLES[r] for r in allowed)} may {what}."

    def _reach(self, user: str, others) -> str | None:
        if self.contacts is None:
            return None
        blocked = sorted(o for o in others if o != user and o not in self.contacts.get(user, ()))
        return f"{user} may not contact {', '.join(blocked)}." if blocked else None

    def send(self, user: str, members, channel: str | None, chat_id: str | None = None) -> str | None:
        """Posting or editing. `channel` is the channel's name, or None in a direct chat."""
        if not channel:
            return self._reach(user, members)
        if self.posters is None:
            return None
        allowed = self.posters.get(channel.lower(), self.posters.get(chat_id or ""))
        if allowed is None or user in allowed:
            return None
        return f"Only {', '.join(sorted(allowed))} may post in #{channel}."

    def react(self, user: str, members, channel: str | None) -> str | None:
        return None if channel else self._reach(user, members)

    def create(self, user: str, members) -> str | None:
        return self._reach(user, members)

    def add(self, user: str, members, new) -> str | None:
        return self._reach(user, new)

    def join(self, user: str, members) -> str | None:
        return None
