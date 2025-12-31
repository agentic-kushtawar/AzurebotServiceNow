# Use Cases (Chat + Voice)

This document collects the supported use cases across Teams chat and voice, plus language coverage.

## Channels
- Teams chat (bot)
- Teams voice/calling (STT + TTS bridge)

## Languages
- English (default)
- Spanish (when enabled)
- German (when enabled)

Note: Hebrew strings exist but are not enabled by default.

## Core Chat Use Cases (LLM + rules)

### Greeting
Example prompts:
- "hi"
- "hello there"
- "good morning"

Expected response:
- Friendly greeting and a hint to ask for help/capabilities.

### Help / Capabilities
Example prompts:
- "help"
- "what can you do?"
- "what are your capabilities?"

Expected response:
- Short list of supported tasks and language support.

### Integration Summary
Example prompts:
- "with which systems are you integrated?"
- "what tools do you support?"

Expected response:
- Systems/services the bot is connected to (Azure AD + ServiceNow).

### Intune Device Status
Example prompts:
- "check device compliance for pilot.user_AndroidForWor"
- "last check-in for device \"pilot.user_AndroidForWor\""
- "check device compliance for all devices"

Behavior:
- Looks up the Intune managed device by name.
- Returns compliance status and last check-in time.
- If "all devices" is requested, returns a summary of the latest 10 devices.

### Intune Remote Actions (Planned)
Example prompts:
- "sync device pilot.user_AndroidForWork_12/20/2025_10:02 PM"
- "lock device pilot.user_AndroidForWork_12/20/2025_10:02 PM"
- "retire device pilot.user_AndroidForWork_12/20/2025_10:02 PM"

Behavior:
- Safe actions to implement next: sync, lock, retire, rename.
- Higher-risk actions (wipe/delete) require explicit approval.

### Create Ticket (ServiceNow)
Example prompts:
- "raise a ticket: vpn not connecting"
- "open an incident for Outlook keeps crashing"

Behavior:
- Creates a ServiceNow incident.
- Returns the incident number and status.
- Shows a confirmation.

### Ticket How-To
Example prompts:
- "how can I raise a ticket?"
- "what should I type to open a ticket?"

Behavior:
- Explains the ticket creation format.

### Ticket Status
Example prompts:
- "status of INC0012345"
- "what happened to inc10024"

Behavior:
- Looks up the incident and returns state + short description.

### VPN Help
Example prompts:
- "vpn keeps disconnecting"
- "vpn not connecting"

Behavior:
- Proposes a ticket and provides VPN tips.

### Password Reset (Microsoft Entra ID)
Example prompts:
- "reset my password"
- "I forgot my password"

Flow (must follow):
1) Ask for username (UPN/email)
2) Validate in Entra ID via Graph
3) Confirm account and recovery email (from Graph methods)
4) Ask consent
5) Redirect to official Microsoft SSPR URL
6) Confirm process initiated

Notes:
- No password reset email is sent by the bot.
- User is always directed to Microsoft’s official SSPR flow.

### Repeat Last Response
Example prompts:
- "repeat that"
- "say that again"

Behavior:
- Replays the most recent response, if available.

### Incident Intelligence (Recurring Incidents)
Example prompts:
- "show recurring incidents for the last 30 days"
- "what problems should I focus on this month?"

Behavior:
- Queries ServiceNow stats, detects repeated issues, shows trends.
- Returns an adaptive-card table in Teams.
- Provides dashboard URL for visuals.

## Voice Use Cases

### Voice Chat (STT -> Orchestrator -> TTS)
All chat intents above are supported in voice.

Voice response notes:
- "help", "capabilities", "bot profile" provide a voice-friendly summary.
- "repeat last" replays the last spoken response.

### Voice Language Switching
Example prompts:
- "switch to Spanish"
- "change language to German"

Behavior:
- Sets the speech recognition + voice response language for the call.

### Hands-Free Lab Dictation (Transcript Only)
Spoken commands (exact):
- Start: "Begin lab note"
- Stop: "Stop recording"
- Confirm upload: "Confirm upload" or "Yes"
- Cancel: "Cancel" or "Discard"

Behavior:
- Max duration: 60 seconds, auto-stop at 60s.
- Transcript is generated and confirmed before upload.
- Only text is stored; audio is discarded.
- Transcript is uploaded to Azure Blob Storage container `lab-transcripts`.

Sample flow:
1) "Begin lab note" -> "Lab note recording started..."
2) Speak 5–60 seconds
3) "Stop recording" -> "Recording stopped... Would you like to share and save it?"
4) "Confirm upload" -> "Your lab note has been saved successfully."

## Language Switching (Chat)
Command:
- "/language es"
- "/language de"

Behavior:
- Switches bot reply language in chat.

## Safety and Loop Avoidance
- Global interrupts: "cancel", "stop", "start over", "help", "reset"
- Password reset flow resets on repeated unexpected input
- State timeout clears stale sessions and returns to open-ended help
