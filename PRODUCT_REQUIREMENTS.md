# Qurocare GMS - v1 Scope

## Purpose

Give the Qurocare Growth team one internal place to manage verified-provider acquisition and onboarding, demonstrate measurable traction to management, and convert field feedback into clear inputs for the Technology team.

## Included workflow

`New Lead -> Contacted -> Meeting Scheduled -> Demo Scheduled -> Demo Completed -> Interested -> Verification -> Agreement Sent -> Onboarding -> Active Provider`

Leads can be individual doctors, nurses, physiotherapists, labs, other medical professionals, or provider organisations.

## Modules

| Module | What it does |
| --- | --- |
| Dashboard | Shows the pipeline, conversion to active providers, due follow-ups, team activity, and recent provider feedback. |
| Provider Leads | Creates and updates a provider lead, including ownership, stage, priority, and next follow-up. |
| Team Activity | Captures daily calls, meetings, demos, follow-ups, and new leads by team member. |
| Provider Feedback | Captures bugs, feature requests, onboarding concerns, and training requirements for the Technology team. |

## Core KPIs

- Total provider leads
- Interested providers
- Demos scheduled/completed
- Providers in onboarding
- Active providers
- Lead-to-active-provider conversion
- Follow-ups due today or overdue
- Calls, meetings, demos, follow-ups, and new leads per team member

## Data fields

### Provider lead

Provider/organisation name; provider type; contact person; phone; email; lead source; assigned team member; pipeline stage; priority; date added; last contact; next follow-up; remarks.

### Team activity

Date; team member; calls; meetings; demos; follow-ups; new leads; notes.

### Provider feedback

Provider; submitted by; date; category; priority; description; Technology owner; status; release version.

## Explicitly outside v1

- Patient records, EMR, prescriptions, reports, or clinical notes
- Patient bookings, billing, or payment data
- Marketplace revenue, cancellations, ratings, and provider utilisation
- Public provider access
- ClickUp/Jira synchronisation
- Production authentication and role-based permissions

## Deployment decision

The prototype uses a local SQLite database. For a shared Qurocare deployment, the Technology team should place the app behind company authentication, move data to a managed database, use encrypted backups, and publish it on `gms.qurocare.com` (preferred) or a protected internal URL.
