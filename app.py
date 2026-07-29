from __future__ import annotations

import json
from datetime import date

import pandas as pd
import streamlit as st

from db import (
    DEFAULT_TEAM, FEEDBACK_CATEGORIES, FEEDBACK_STATUSES, PRIORITIES, PROVIDER_TYPES,
    ROLE_BDM, ROLE_CEO, ROLE_ML, ROLE_MO, ROLE_PSM, SOURCES, STAGES, authenticate,
    create_user, execute, frame, initialise, user_count, user_label, users_frame,
)

st.set_page_config(page_title="Qurocare GMS", page_icon=":material/health_and_safety:", layout="wide")
initialise()
st.session_state.setdefault("gms_user", None)

ACTIVITY_FIELDS = {
    ROLE_ML: [
        ("providers_researched", "Providers researched"),
        ("leads_qualified", "Leads qualified"),
        ("leads_submitted", "Leads submitted to Rahul"),
    ],
    ROLE_BDM: [
        ("calls_attempted", "Calls attempted"),
        ("contacts_connected", "Contacts connected"),
        ("interested_leads", "Interested leads"),
        ("demos_scheduled", "Demos scheduled"),
        ("followups_completed", "Follow-ups completed"),
    ],
    ROLE_PSM: [
        ("demos_conducted", "Demos conducted"),
        ("feedback_logged", "Provider feedback logged"),
        ("tech_followups", "Tech follow-ups"),
        ("activation_coordination", "Activation coordination"),
    ],
    ROLE_MO: [
        ("demos_supported", "Demos supported"),
        ("documents_reviewed", "Documents reviewed"),
        ("verifications_completed", "Verifications completed"),
        ("compliance_completed", "Legal/compliance packs completed"),
        ("onboarding_ready", "Providers ready for onboarding"),
    ],
    ROLE_CEO: [],
}


def current_user() -> dict:
    return st.session_state["gms_user"]


def is_management(user: dict) -> bool:
    return user["role"] in {ROLE_PSM, ROLE_CEO}


def export_button(data: pd.DataFrame, name: str) -> None:
    st.download_button("Download CSV", data.to_csv(index=False).encode("utf-8"), f"{name}.csv", "text/csv")


def choose_or_specify(label: str, options: list[str], key: str, current: str | None = None) -> str:
    custom_value = ""
    if current and current not in options:
        index, custom_value = options.index("Other"), current
    else:
        index = options.index(current) if current in options else 0
    selection = st.selectbox(label, options, index=index, key=key)
    custom = st.text_input(f"{label} - if Other, please specify", value=custom_value, key=f"{key}_other")
    return custom.strip() if selection == "Other" and custom.strip() else selection


def my_provider_query(user: dict) -> tuple[str, tuple]:
    if user["role"] == ROLE_CEO:
        return "SELECT * FROM providers ORDER BY next_follow_up ASC, id DESC", ()
    return """SELECT * FROM providers
              WHERE created_by_user_id = ? OR assigned_to_user_id = ?
              ORDER BY next_follow_up ASC, id DESC""", (user["id"], user["id"])


def setup_first_admin() -> None:
    st.title("Set up Qurocare GMS")
    st.info("Create the first local pilot account for Reshma, PSM. Before web deployment, this local login will be replaced by Supabase Auth.")
    with st.form("first_admin"):
        email = st.text_input("Reshma's work email")
        password = st.text_input("Create password", type="password")
        confirm = st.text_input("Confirm password", type="password")
        if st.form_submit_button("Create first account", type="primary"):
            if not email.strip() or len(password) < 8:
                st.error("Enter a work email and a password of at least 8 characters.")
            elif password != confirm:
                st.error("Passwords do not match.")
            else:
                create_user("Reshma", ROLE_PSM, email, password)
                reshma = authenticate(email, password)
                # Preserve existing pilot records by making the first Growth Lead
                # their accountable owner. Future records use the logged-in user.
                execute("UPDATE providers SET created_by_user_id=?, assigned_to_user_id=?, assigned_to=?, updated_by_user_id=? WHERE created_by_user_id IS NULL AND assigned_to_user_id IS NULL", (reshma["id"], reshma["id"], user_label(reshma), reshma["id"]))
                execute("UPDATE feedback SET submitted_by_user_id=?, updated_by_user_id=? WHERE submitted_by_user_id IS NULL", (reshma["id"], reshma["id"]))
                st.success("Account created. Please sign in.")
                st.rerun()


def login_page() -> None:
    st.title("Qurocare Growth Management System")
    st.caption("Internal provider growth and onboarding portal")
    with st.container(border=True):
        st.subheader("Sign in")
        with st.form("login"):
            email = st.text_input("Work email")
            password = st.text_input("Password", type="password")
            if st.form_submit_button("Sign in", type="primary"):
                user = authenticate(email, password)
                if user:
                    st.session_state["gms_user"] = user
                    st.rerun()
                else:
                    st.error("Invalid email or password.")
    st.caption("Local pilot login only. Use Supabase Auth before deployment to gms.qurocare.com.")


def dashboard_page(user: dict) -> None:
    st.title("Growth operations dashboard")
    st.caption("Shared operational view. Individual records remain restricted to their owner.")
    providers = frame("SELECT * FROM providers ORDER BY date_added DESC")
    total = len(providers)
    active = int((providers.stage == "Active Provider").sum()) if total else 0
    demos = int(providers.stage.isin(["Demo Scheduled", "Demo Completed"]).sum()) if total else 0
    onboarding = int((providers.stage == "Onboarding").sum()) if total else 0
    conversion = active / total * 100 if total else 0
    due_dates = pd.to_datetime(providers.get("next_follow_up"), errors="coerce") if total else pd.Series(dtype="datetime64[ns]")
    due = int((due_dates.dt.date <= date.today()).sum()) if not due_dates.empty else 0
    with st.container(horizontal=True):
        st.metric("Total leads", total, border=True)
        st.metric("Demos", demos, border=True)
        st.metric("Onboarding", onboarding, border=True)
        st.metric("Active providers", active, border=True)
        st.metric("Conversion", f"{conversion:.1f}%", border=True)
        st.metric("Follow-ups due", due, border=True)

    col_a, col_b = st.columns((1.2, 1))
    with col_a:
        with st.container(border=True):
            st.subheader("Provider pipeline")
            pipeline = providers.groupby("stage").size().reindex(STAGES, fill_value=0).reset_index(name="Providers")
            pipeline.columns = ["Stage", "Providers"]
            st.bar_chart(pipeline, x="Stage", y="Providers")
    with col_b:
        with st.container(border=True):
            st.subheader("Team scorecards")
            scorecards = build_scorecards()
            st.dataframe(scorecards, hide_index=True, width="stretch")

    with st.container(border=True):
        st.subheader("Follow-ups requiring attention")
        if total:
            rows = providers.assign(Follow_up=due_dates)
            rows = rows[rows.Follow_up.dt.date <= date.today()][["company_name", "contact_name", "assigned_to", "stage", "Follow_up", "remarks"]]
            rows.columns = ["Provider", "Contact", "Current owner", "Stage", "Follow-up", "Remarks"]
            st.dataframe(rows, hide_index=True, width="stretch")
        else:
            st.info("No provider records yet.")


def build_scorecards() -> pd.DataFrame:
    users = users_frame()
    activities = frame("SELECT ra.*, u.name, u.role FROM role_activities ra JOIN users u ON u.id = ra.user_id")
    reviews = frame("SELECT submitted_by_user_id, outcome FROM handoff_reviews")
    rows = []
    for user in users.itertuples():
        metrics = {}
        if not activities.empty:
            for value in activities[activities.user_id == user.id].metrics_json:
                metrics.update({k: metrics.get(k, 0) + int(v) for k, v in json.loads(value).items()})
        if user.role == ROLE_ML:
            subset = reviews[reviews.submitted_by_user_id == user.id] if not reviews.empty else pd.DataFrame()
            reviewed = len(subset)
            accepted = int((subset.outcome == "Accepted").sum()) if reviewed else 0
            measure = f"{(accepted / reviewed * 100) if reviewed else 0:.0f}% lead acceptance"
            evidence = f"{metrics.get('leads_submitted', 0)} leads submitted"
        elif user.role == ROLE_BDM:
            connected = metrics.get("contacts_connected", 0)
            demos = metrics.get("demos_scheduled", 0)
            measure = f"{(demos / connected * 100) if connected else 0:.0f}% demo conversion"
            evidence = f"{demos} demos scheduled"
        elif user.role == ROLE_MO:
            docs = metrics.get("documents_reviewed", 0)
            verified = metrics.get("verifications_completed", 0)
            measure = f"{(verified / docs * 100) if docs else 0:.0f}% verification completion"
            evidence = f"{metrics.get('compliance_completed', 0)} compliance packs"
        elif user.role == ROLE_PSM:
            measure = f"{metrics.get('demos_conducted', 0)} demos conducted"
            evidence = f"{metrics.get('feedback_logged', 0)} feedback items"
        else:
            measure, evidence = "Management review", "CEO/Admin view"
        rows.append({"Team member": f"{user.name}, {user.role}", "Main measure": measure, "Evidence": evidence})
    return pd.DataFrame(rows)


def my_leads_page(user: dict) -> None:
    st.title("My provider leads")
    st.caption("Only leads you created or currently own are listed here. Names are taken from your login.")
    query, params = my_provider_query(user)
    providers = frame(query, params)
    with st.expander("Add provider lead", expanded=False):
        with st.form("new_provider", clear_on_submit=True):
            a, b, c = st.columns(3)
            company = a.text_input("Provider / organisation name *")
            provider_type = choose_or_specify("Provider type *", PROVIDER_TYPES, "new_type")
            contact = c.text_input("Contact person")
            a, b, c = st.columns(3)
            phone = a.text_input("Phone")
            email = b.text_input("Email")
            source = choose_or_specify("Lead source", SOURCES, "new_source")
            a, b, c = st.columns(3)
            stage = a.selectbox("Current stage", STAGES, index=STAGES.index("Research") if user["role"] == ROLE_ML else STAGES.index("New Lead"))
            priority = b.selectbox("Priority", PRIORITIES, index=1)
            followup = c.date_input("Next follow-up", value=date.today())
            notes = st.text_area("Remarks")
            st.caption(f"Owner: {user_label(user)}")
            if st.form_submit_button("Save provider lead", type="primary"):
                if not company.strip():
                    st.error("Provider name is required.")
                else:
                    execute("""INSERT INTO providers (company_name, provider_type, contact_name, phone, email, source, assigned_to, assigned_to_user_id, created_by_user_id, updated_by_user_id, stage, priority, date_added, next_follow_up, remarks, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""", (company.strip(), provider_type, contact.strip(), phone.strip(), email.strip(), source, user_label(user), user["id"], user["id"], user["id"], stage, priority, date.today().isoformat(), followup.isoformat(), notes.strip()))
                    st.success("Provider lead saved.")
                    st.rerun()
    st.subheader("My lead register")
    if providers.empty:
        st.info("No leads are assigned to you yet.")
        return
    export_button(providers, "my-provider-leads")
    show = providers[["id", "company_name", "provider_type", "contact_name", "phone", "source", "stage", "priority", "next_follow_up", "remarks"]].copy()
    show.columns = ["ID", "Provider", "Type", "Contact", "Phone", "Source", "Stage", "Priority", "Follow-up", "Remarks"]
    st.dataframe(show, hide_index=True, width="stretch")
    lead_ids = {int(row.id): f"#{row.id} - {row.company_name}" for row in providers.itertuples()}
    selected_id = st.selectbox("Edit my provider lead", list(lead_ids), format_func=lambda x: lead_ids[x])
    record = providers[providers.id == selected_id].iloc[0]
    with st.form("edit_provider"):
        a, b, c = st.columns(3)
        company = a.text_input("Provider / organisation name *", value=record.company_name, key=f"provider_name_{selected_id}")
        provider_type = choose_or_specify("Provider type *", PROVIDER_TYPES, f"provider_type_{selected_id}", record.provider_type)
        contact = c.text_input("Contact person", value=record.contact_name or "", key=f"provider_contact_{selected_id}")
        a, b, c = st.columns(3)
        phone = a.text_input("Phone", value=record.phone or "", key=f"provider_phone_{selected_id}")
        email = b.text_input("Email", value=record.email or "", key=f"provider_email_{selected_id}")
        source = choose_or_specify("Lead source", SOURCES, f"provider_source_{selected_id}", record.source)
        a, b, c = st.columns(3)
        stage = a.selectbox("Stage", STAGES, index=STAGES.index(record.stage), key=f"provider_stage_{selected_id}")
        priority = b.selectbox("Priority", PRIORITIES, index=PRIORITIES.index(record.priority), key=f"provider_priority_{selected_id}")
        followup = c.date_input("Next follow-up", value=pd.to_datetime(record.next_follow_up).date() if pd.notna(record.next_follow_up) else date.today(), key=f"provider_followup_{selected_id}")
        notes = st.text_area("Remarks", value=record.remarks or "", key=f"provider_notes_{selected_id}")
        if st.form_submit_button("Save my changes", type="primary"):
            execute("""UPDATE providers SET company_name=?, provider_type=?, contact_name=?, phone=?, email=?, source=?, stage=?, priority=?, next_follow_up=?, remarks=?, last_contact=?, updated_by_user_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""", (company.strip(), provider_type, contact.strip(), phone.strip(), email.strip(), source, stage, priority, followup.isoformat(), notes.strip(), date.today().isoformat(), user["id"], selected_id))
            st.success("Your provider lead was updated.")
            st.rerun()


def my_activity_page(user: dict) -> None:
    st.title("My activity")
    fields = ACTIVITY_FIELDS[user["role"]]
    if not fields:
        st.info("CEO/Admin accounts do not submit operational activity.")
        return
    st.caption(f"Your activity is saved as {user_label(user)}. Only you can edit these entries.")
    with st.form("new_activity", clear_on_submit=True):
        activity_date = st.date_input("Activity date", value=date.today())
        columns = st.columns(min(3, len(fields)))
        values = {}
        for index, (key, label) in enumerate(fields):
            values[key] = columns[index % len(columns)].number_input(label, min_value=0, value=0, key=f"new_{key}")
        notes = st.text_area("Notes")
        if st.form_submit_button("Save my activity", type="primary"):
            execute("INSERT INTO role_activities (user_id, activity_date, metrics_json, notes) VALUES (?, ?, ?, ?)", (user["id"], activity_date.isoformat(), json.dumps(values), notes.strip()))
            st.success("Activity saved.")
            st.rerun()
    data = frame("SELECT id, activity_date, metrics_json, notes FROM role_activities WHERE user_id=? ORDER BY activity_date DESC, id DESC", (user["id"],))
    st.subheader("My activity history")
    if data.empty:
        st.info("No activity submitted yet.")
        return
    display = data.copy()
    for key, label in fields:
        display[label] = display.metrics_json.apply(lambda value: json.loads(value).get(key, 0))
    display = display.drop(columns=["id", "metrics_json"])
    export_button(display, "my-activity")
    st.dataframe(display, hide_index=True, width="stretch")
    record_id = st.selectbox("Edit my activity entry", data.id.tolist(), format_func=lambda x: f"Activity #{x}")
    record = data[data.id == record_id].iloc[0]
    previous = json.loads(record.metrics_json)
    with st.form("edit_activity"):
        activity_date = st.date_input("Activity date", value=pd.to_datetime(record.activity_date).date(), key=f"activity_date_{record_id}")
        columns = st.columns(min(3, len(fields)))
        values = {}
        for index, (key, label) in enumerate(fields):
            values[key] = columns[index % len(columns)].number_input(label, min_value=0, value=int(previous.get(key, 0)), key=f"activity_{key}_{record_id}")
        notes = st.text_area("Notes", value=record.notes or "", key=f"activity_notes_{record_id}")
        if st.form_submit_button("Save my activity changes", type="primary"):
            execute("UPDATE role_activities SET activity_date=?, metrics_json=?, notes=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?", (activity_date.isoformat(), json.dumps(values), notes.strip(), record_id, user["id"]))
            st.success("Your activity entry was updated.")
            st.rerun()


def my_feedback_page(user: dict) -> None:
    st.title("My provider feedback")
    providers = frame(*my_provider_query(user))
    if providers.empty:
        st.info("Add or receive a provider lead before recording provider feedback.")
        return
    names = providers.company_name.tolist()
    with st.form("new_feedback", clear_on_submit=True):
        a, b, c = st.columns(3)
        provider = a.selectbox("Provider", names)
        category = choose_or_specify("Category", FEEDBACK_CATEGORIES, "feedback_category")
        priority = c.selectbox("Priority", PRIORITIES, index=1)
        description = st.text_area("Feedback / issue *")
        tech_owner = st.text_input("Technology owner", value="Tech Team")
        st.caption(f"Submitted by: {user_label(user)}")
        if st.form_submit_button("Save my feedback", type="primary"):
            if not description.strip():
                st.error("Feedback description is required.")
            else:
                execute("""INSERT INTO feedback (provider_name, submitted_by, submitted_by_user_id, feedback_date, category, priority, description, assigned_to, status, release_version, updated_by_user_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'New', '', ?, CURRENT_TIMESTAMP)""", (provider, user_label(user), user["id"], date.today().isoformat(), category, priority, description.strip(), tech_owner.strip(), user["id"]))
                st.success("Feedback saved.")
                st.rerun()
    data = frame("SELECT id, feedback_date, provider_name, category, priority, description, assigned_to, status, release_version FROM feedback WHERE submitted_by_user_id=? ORDER BY feedback_date DESC, id DESC", (user["id"],))
    st.subheader("My feedback history")
    if data.empty:
        st.info("No feedback submitted yet.")
        return
    st.dataframe(data.drop(columns="id"), hide_index=True, width="stretch")
    feedback_id = st.selectbox("Edit my feedback", data.id.tolist(), format_func=lambda x: f"Feedback #{x}")
    record = data[data.id == feedback_id].iloc[0]
    with st.form("edit_feedback"):
        category = choose_or_specify("Category", FEEDBACK_CATEGORIES, f"edit_feedback_category_{feedback_id}", record.category)
        priority = st.selectbox("Priority", PRIORITIES, index=PRIORITIES.index(record.priority), key=f"feedback_priority_{feedback_id}")
        status = st.selectbox("Status", FEEDBACK_STATUSES, index=FEEDBACK_STATUSES.index(record.status), key=f"feedback_status_{feedback_id}")
        tech_owner = st.text_input("Technology owner", value=record.assigned_to or "", key=f"feedback_owner_{feedback_id}")
        release = st.text_input("Release version", value=record.release_version or "", key=f"feedback_release_{feedback_id}")
        description = st.text_area("Feedback / issue *", value=record.description, key=f"feedback_description_{feedback_id}")
        if st.form_submit_button("Save my feedback changes", type="primary"):
            execute("UPDATE feedback SET category=?, priority=?, status=?, assigned_to=?, release_version=?, description=?, updated_by_user_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND submitted_by_user_id=?", (category, priority, status, tech_owner.strip(), release.strip(), description.strip(), user["id"], feedback_id, user["id"]))
            st.success("Your feedback entry was updated.")
            st.rerun()


def handoffs_page(user: dict) -> None:
    st.title("Handoff reviews")
    role = user["role"]
    if role == ROLE_BDM:
        queue = frame("""SELECT p.* FROM providers p JOIN users u ON u.id=p.created_by_user_id
                       WHERE p.stage='Research' AND u.role=? ORDER BY p.date_added""", (ROLE_ML,))
        heading, handoff_type = "Research leads awaiting outreach review", "Research to outreach"
    elif role in {ROLE_PSM, ROLE_MO}:
        queue = frame("""SELECT p.* FROM providers p JOIN users u ON u.id=p.assigned_to_user_id
                       WHERE p.stage='Demo Scheduled' AND u.role=? ORDER BY p.date_added""", (ROLE_BDM,))
        heading, handoff_type = "Demo-ready leads awaiting review", "Outreach to demo"
    else:
        queue = pd.DataFrame()
        heading, handoff_type = "No handoff queue for this role", ""
    st.subheader(heading)
    if queue.empty:
        st.info("No handoffs are waiting for your review.")
    else:
        st.dataframe(queue[["company_name", "contact_name", "stage", "remarks"]], hide_index=True, width="stretch")
        options = {int(row.id): f"#{row.id} - {row.company_name}" for row in queue.itertuples()}
        provider_id = st.selectbox("Select handoff", list(options), format_func=lambda x: options[x])
        outcome = st.selectbox("Review outcome", ["Accepted", "Needs rework", "Rejected"])
        evidence = st.text_area("Reason / evidence")
        if st.button("Save handoff review", type="primary"):
            submitted = int(queue[queue.id == provider_id].iloc[0].created_by_user_id or queue[queue.id == provider_id].iloc[0].assigned_to_user_id)
            execute("INSERT INTO handoff_reviews (provider_id, submitted_by_user_id, reviewed_by_user_id, handoff_type, outcome, review_date, evidence) VALUES (?, ?, ?, ?, ?, ?, ?)", (provider_id, submitted, user["id"], handoff_type, outcome, date.today().isoformat(), evidence.strip()))
            if outcome == "Accepted" and role == ROLE_BDM:
                execute("UPDATE providers SET assigned_to=?, assigned_to_user_id=?, stage='New Lead', updated_by_user_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (user_label(user), user["id"], user["id"], provider_id))
            st.success("Handoff review saved.")
            st.rerun()
    my_reviews = frame("""SELECT h.review_date, p.company_name, h.handoff_type, h.outcome, h.evidence
                          FROM handoff_reviews h JOIN providers p ON p.id=h.provider_id
                          WHERE h.reviewed_by_user_id=? ORDER BY h.review_date DESC, h.id DESC""", (user["id"],))
    if not my_reviews.empty:
        st.subheader("My review history")
        st.dataframe(my_reviews, hide_index=True, width="stretch")


def user_management_page(user: dict) -> None:
    st.title("Team access")
    st.caption("Only Reshma, PSM and CEO/Admin can create local pilot accounts.")
    with st.form("new_user", clear_on_submit=True):
        team_options = [f"{name}|{role}" for name, role in DEFAULT_TEAM] + [f"CEO|{ROLE_CEO}"]
        selected = st.selectbox("Team member", team_options)
        name, role = selected.split("|", 1)
        email = st.text_input("Work email")
        password = st.text_input("Temporary password", type="password")
        if st.form_submit_button("Create account", type="primary"):
            if not email.strip() or len(password) < 8:
                st.error("Enter a work email and a temporary password of at least 8 characters.")
            else:
                try:
                    create_user(name, role, email, password)
                    st.success(f"Account created for {name}.")
                except Exception:
                    st.error("An account with that email already exists.")
    st.subheader("Current accounts")
    st.dataframe(users_frame().drop(columns=["id"]), hide_index=True, width="stretch")


def main_app() -> None:
    user = current_user()
    with st.sidebar:
        st.title("Qurocare GMS")
        st.caption(user_label(user))
        if st.button("Sign out", icon=":material/logout:"):
            st.session_state["gms_user"] = None
            st.rerun()
        st.divider()
        pages = ["Dashboard", "My provider leads", "My activity", "My provider feedback", "Handoff reviews"]
        if is_management(user):
            pages.append("Team access")
        page = st.radio("Navigate", pages)
        st.caption("Local pilot - migrate to Supabase Auth before deployment.")
    if page == "Dashboard":
        dashboard_page(user)
    elif page == "My provider leads":
        my_leads_page(user)
    elif page == "My activity":
        my_activity_page(user)
    elif page == "My provider feedback":
        my_feedback_page(user)
    elif page == "Handoff reviews":
        handoffs_page(user)
    elif page == "Team access":
        user_management_page(user)


if user_count() == 0:
    setup_first_admin()
elif current_user() is None:
    login_page()
else:
    main_app()
