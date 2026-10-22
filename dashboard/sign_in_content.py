"""Text blocks for the sign-in page (E1): the developer credit and the password-help panel.

Plain functions returning escaped HTML, so they are tested without Streamlit. The page
styles them (``.lf-credit``, ``.lf-recovery``).

Password help is honest about how accounts work here: there is no sign-up, no email and
no reset link. An administrator creates accounts and sets new passwords on the server
(``manage_users set-password``), which also ends the account's other sessions. The panel
asks for nothing, so it reveals nothing about which accounts exist.
"""

import html

DEVELOPER_NAME = "Hamaz Mubashar"
DEVELOPER_TITLE = "AI Automation & LLM Engineer"
RESET_COMMAND = "python -m leadflow_ai.manage_users set-password <username>"
DOCKER_PREFIX = "docker compose exec api"


def _command(text: str) -> str:
    """A shell command that wraps only between words (copying it still gives ``text``)."""
    return " ".join(f'<span class="lf-nw">{html.escape(word)}</span>' for word in text.split())


def credit_html(url: str | None) -> str:
    """``Built by Hamaz Mubashar · AI Automation & LLM Engineer``; the name links to ``url``.

    ``url`` comes from validated settings (https only); it is still escaped here. With no
    URL the credit is plain text.
    """
    name = html.escape(DEVELOPER_NAME)
    if url:
        name = (
            f'<a href="{html.escape(url, quote=True)}" target="_blank" '
            f'rel="noopener noreferrer">{name}<span class="lf-sr"> (opens in a new tab)</span></a>'
        )
    return (
        f'<footer class="lf-credit">Built by {name}<span aria-hidden="true"> · </span>'
        f"{html.escape(DEVELOPER_TITLE)}</footer>"
    )


def recovery_html(*, demo_available: bool) -> str:
    """The "Forgot password?" panel: what to do, with no form and no account lookup."""
    demo = (
        '<p class="lf-recovery-note"><b>Just looking around?</b> You don\'t need an account: '
        "go back and choose <b>Explore the live demo</b>.</p>"
        if demo_available
        else ""
    )
    return (
        '<section class="lf-recovery" aria-labelledby="lf-recovery-title">'
        '<div class="lf-h1" id="lf-recovery-title" role="heading" aria-level="1">'
        "Reset your password</div>"
        "<p>LeadFlow AI accounts are created by an administrator, so there is no sign-up "
        "page and no email reset link. An administrator sets your new password.</p>"
        '<ol class="lf-steps">'
        "<li><b>Contact your LeadFlow AI administrator</b> and tell them your username. "
        "Never send them your old password: they don't need it.</li>"
        "<li><b>The administrator sets a new password</b> on the server:"
        f"<code>{_command(RESET_COMMAND)}</code>"
        f"<small>On a Docker deployment, start it with <code>{_command(DOCKER_PREFIX)}"
        "</code>. The new password is typed at a hidden prompt.</small></li>"
        "<li><b>Sign in with the new password.</b> Any other signed-in sessions of your "
        "account end automatically.</li>"
        "</ol>"
        '<p class="lf-recovery-note">If sign-in is locked after failed attempts, wait for '
        "the countdown to finish before you try the new password.</p>"
        f"{demo}</section>"
    )
