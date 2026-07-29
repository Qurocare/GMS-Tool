# Qurocare Growth Management System

Internal portal for provider acquisition, onboarding, team activity, and product feedback.

## Run locally

```powershell
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Open the local URL shown by Streamlit (usually `http://localhost:8501`).

## Included in v1

- Live provider acquisition dashboard
- Role-aware local login and account setup
- Personal provider-lead, activity, and feedback registers
- Role-specific activity forms for Market Research, Business Development, PSM, and Medical Officer work
- Handoff reviews for research-to-outreach and outreach-to-demo quality
- Product feedback register for the Tech team
- CSV exports from each operating page

## First-time setup

1. Start the app and create the first account for **Reshma, PSM**.
2. Sign in as Reshma and open **Team access** in the sidebar.
3. Create accounts for Dr. Asinsha, Halifa, Rahul, and the CEO/Admin using their work emails and temporary passwords.
4. Each team member signs in with their own account. Their name is derived from the login; they cannot select another team member in forms or edit another person's activity/feedback.

## Data and security

The app stores data in `data/gms.db` on the machine running it. This version intentionally does not store patient records, EMR data, prescriptions, or billing information. The local login is for pilot testing only. Before deploying to a shared Qurocare URL, move to Supabase Postgres and replace local authentication with Supabase Auth plus role-based database policies.
