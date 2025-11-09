# core/orchestrator/playbooks.py
from __future__ import annotations
import os
from typing import Dict

PORTAL_URL   = os.getenv("PASSWORD_RESET_URL", "https://selfservice.example.com/reset")
HELP_EMAIL   = os.getenv("HELPDESK_EMAIL", "helpdesk@example.com")
HELP_PHONE   = os.getenv("HELPDESK_PHONE", "+00 1234 567890")
AD_SYNC_MINS = os.getenv("AD_SYNC_MINUTES", "5")

def password_reset_playbook(user: Dict[str, str]) -> str:
    name = (user.get("user_name") or "there").split()[0]
    return (
        f"Hi {name}, here’s the quickest way to reset your password:\n"
        f"1) Open {PORTAL_URL}\n"
        f"2) Verify your identity (MFA/SMS/email).\n"
        f"3) Set a new password (meet complexity rules).\n"
        f"4) Wait ~{AD_SYNC_MINS} minutes for systems to sync.\n"
        f"5) Re-login to VPN, Outlook/Teams, and any mapped drives.\n\n"
        f"If you’re locked out or MFA isn’t working, contact Service Desk:\n"
        f"- Email: {HELP_EMAIL}\n"
        f"- Phone: {HELP_PHONE}"
    )

def help_playbook() -> str:
    return (
        "You can try:\n"
        "- “raise a ticket: <reason>”\n"
        "- “status of INC0012345”\n"
        "- “reset my password”\n"
        "- “vpn not connecting”"
    )

def vpn_tip_playbook() -> str:
    return (
        "Quick VPN checks:\n"
        "1) Toggle Wi-Fi or switch network.\n"
        "2) Quit & relaunch the VPN client.\n"
        "3) Verify time/date are correct.\n"
        "4) If still failing, I can raise a ticket for you."
    )

def ticket_propose_playbook() -> str:
    """
    Generic proposal / how-to tips shown when user says e.g. 'open a ticket' without details.
    """
    return (
        "Here’s how to proceed:\n"
        "• Create now: **create_ticket: <short reason>**\n"
        "   e.g., create_ticket: Outlook keeps crashing\n"
        "• Or say: **open a ticket: <short reason>**\n"
        "• Check later: **status of INC0012345**"
    )
