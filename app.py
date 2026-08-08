from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from db import (
    DEFAULT_TEAM, FEEDBACK_CATEGORIES, FEEDBACK_STATUSES, PRIORITIES, PROVIDER_TYPES,
    ROLE_BDM, ROLE_CEO, ROLE_DISPLAY, ROLE_ML, ROLE_MO, ROLE_PGA, ROLE_PSM, SOURCES, STAGES, authenticate,
    create_user, execute, frame, initialise, user_count, user_label, users_frame,
)

st.set_page_config(page_title="Qurocare GMS", page_icon=":material/health_and_safety:", layout="wide")
initialise()
st.session_state.setdefault("gms_user", None)

ACTIVITY_FIELDS = {
    ROLE_ML: [
        ("providers_researched", "Providers researched"),
        ("meaningful_conversations", "Meaningful provider conversations (M)"),
        ("leads_qualified", "Qualified interested leads added to GMS (QIL)"),
        ("demos_supported", "Demos supported"),
        ("lost_cases_handled", "Lost cases handled (re-engagement calls)"),
        ("followups_completed", "Follow-ups completed"),
    ],
    ROLE_BDM: [
        ("qualified_leads_contacted", "Qualified leads contacted (QLC)"),
        ("demos_completed", "Demos completed (does not by itself mean converted)"),
        ("converted_leads", "Converted leads - moved to Converted stage (C)"),
        ("followups_completed", "Follow-ups completed"),
    ],
    ROLE_PSM: [
        ("demos_conducted", "Demos conducted"),
        ("providers_ready_for_onboarding", "Verified providers ready for onboarding (V)"),
        ("providers_ready_to_activate", "Providers ready to activate (AP)"),
        ("feedback_logged", "Provider feedback logged"),
        ("tech_followups", "Tech follow-ups"),
    ],
    ROLE_MO: [
        ("demos_supported", "Demos supported"),
        ("converted_leads_received", "Converted leads received for verification (C)"),
        ("documents_reviewed", "Documents reviewed"),
        ("verifications_completed", "Verifications completed (V)"),
        ("followups_completed", "Follow-ups completed"),
    ],
    ROLE_PGA: [],
    ROLE_CEO: [],
}


def current_user() -> dict:
    return st.session_state["gms_user"]


def is_management(user: dict) -> bool:
    return user["role"] in {ROLE_PSM, ROLE_CEO}


def owner_for_role(role: str) -> tuple[str, int] | None:
    """Return the active GMS account responsible for a workflow role."""
    users = frame(
        "SELECT id, name, role FROM users WHERE role=? AND is_active=1 ORDER BY id LIMIT 1",
        (role,),
    )
    if users.empty:
        return None
    owner = users.iloc[0]
    # Use bracket access for the 'name' column: attribute access (owner.name)
    # collides with pandas' built-in Series.name property (the row's index),
    # which silently returned a row number instead of the person's name.
    return f"{owner['name']}, {owner['role']}", int(owner['id'])


def workflow_owner_for_stage(stage: str) -> tuple[str, int] | None:
    """Return the next owner when a provider reaches a handoff stage.

    Ownership moves automatically at four defined handoff points:
    - Rahul (BDM) -> Dr. Asinsha (MO) when Converted is selected (a lead can
      complete a demo without being converted, so the BDM->MO hand-off now
      happens at Converted, not at Demo Completed or Verification).
    - Dr. Asinsha (MO) -> Reshma (PSM) when Onboarding is selected.
    - Anyone -> Halifa (Market Lead) when Lost is selected, so she can
      re-engage the lead rather than it sitting orphaned with whoever last
      touched it.
    - Halifa (Market Lead) -> Rahul (BDM) when Interested is selected, so a
      lead she re-engages routes straight back into Rahul's pipeline.
    Every other stage change keeps the current owner so the provider does
    not disappear from their Provider Leads page mid-workflow.
    """
    handoff_roles = {
        "Converted": ROLE_MO,
        "Onboarding": ROLE_PSM,
        "Lost": ROLE_ML,
        "Interested": ROLE_BDM,
    }
    role = handoff_roles.get(stage)
    return owner_for_role(role) if role else None


def allowed_stages_for_role(user: dict, current_stage: str) -> list[str]:
    """Limit workflow changes to the stages owned by the signed-in role."""
    allowed_by_role = {
        ROLE_BDM: ["Interested", "Contacted", "Meeting Scheduled", "Demo Scheduled", "Demo Completed", "Converted", "Lost"],
        ROLE_MO: ["Converted", "Verification", "Agreement Sent", "Onboarding", "Lost"],
        ROLE_PSM: ["Onboarding", "Active Provider", "Lost"],
        ROLE_ML: ["Lost", "Interested"],
        ROLE_CEO: STAGES,
    }
    allowed = allowed_by_role.get(user["role"], [current_stage])
    # Keep legacy records editable by their current owner while preserving the
    # normal options for all new workflow records.
    return list(dict.fromkeys([current_stage, *allowed]))


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


def my_provider_query(user: dict, include_lost: bool = True) -> tuple[str, tuple]:
    """Providers currently owned by this user.

    By default this includes Lost providers (used by pages like My provider
    feedback that may still reference them). The Provider Leads page passes
    include_lost=False so Lost providers stop appearing there, while they
    remain visible on the Dashboard and in exported reports, which query the
    providers table directly.
    """
    conditions: list[str] = []
    params: list = []
    if user["role"] != ROLE_CEO:
        conditions.append("assigned_to_user_id = ?")
        params.append(user["id"])
    if not include_lost:
        conditions.append("stage != 'Lost'")
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return f"SELECT * FROM providers{where} ORDER BY next_follow_up ASC, id DESC", tuple(params)


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
    # Temporary display value. Replace with the agreed aggregate provider-app
    # data source after the Tech team confirms the DAP event definition.
    dap_today = 0
    total = len(providers)
    active = int((providers.stage == "Active Provider").sum()) if total else 0
    demos = int(providers.stage.isin(["Demo Scheduled", "Demo Completed"]).sum()) if total else 0
    converted = int((providers.stage == "Converted").sum()) if total else 0
    onboarding = int((providers.stage == "Onboarding").sum()) if total else 0
    conversion = active / total * 100 if total else 0
    due_dates = pd.to_datetime(providers.get("next_follow_up"), errors="coerce") if total else pd.Series(dtype="datetime64[ns]")
    due = int((due_dates.dt.date <= date.today()).sum()) if not due_dates.empty else 0
    with st.container(horizontal=True):
        st.metric("Total leads", total, border=True)
        st.metric("Demos", demos, border=True, help="Demo Scheduled + Demo Completed. A completed demo does not by itself mean the lead converted.")
        st.metric("Converted", converted, border=True, help="Leads Rahul has moved to Converted, now with Dr. Asinsha awaiting Verification.")
        st.metric("Onboarding", onboarding, border=True)
        st.metric("Active providers", active, border=True)
        st.metric("Daily active providers (DAP)", dap_today, border=True,
                  help="Currently shown as 0 until the provider-app data integration is available.")
        st.metric("Conversion", f"{conversion:.1f}%", border=True)
        st.metric("Follow-ups due", due, border=True)
    st.caption("DAP is temporarily set to 0. It will update automatically after Tech connects the agreed provider-app data source.")

    st.subheader("Team performance period")
    period = st.segmented_control(
        "Choose scorecard period",
        ["Today", "This week", "This month", "Custom range"],
        default="Today",
        key="scorecard_period",
    )
    today = date.today()
    if period == "This week":
        start_date, end_date = today - timedelta(days=today.weekday()), today
    elif period == "This month":
        start_date, end_date = today.replace(day=1), today
    elif period == "Custom range":
        selected_range = st.date_input(
            "Choose start and end dates",
            value=(today - timedelta(days=6), today),
            max_value=today,
            key="scorecard_custom_range",
        )
        if isinstance(selected_range, tuple) and len(selected_range) == 2:
            start_date, end_date = selected_range
        else:
            start_date = end_date = selected_range if isinstance(selected_range, date) else today
    else:
        start_date = end_date = today
    st.caption(f"Scorecards below use activity submitted from {start_date.strftime('%d %b %Y')} to {end_date.strftime('%d %b %Y')}.")

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
            scorecards = build_scorecards(start_date, end_date, dap_today)
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

    with st.container(border=True):
        st.subheader("Shared provider register")
        st.caption("Read-only provider pipeline for the full Growth team. Download it when you need a working copy.")
        register = providers[[
            "id", "company_name", "provider_type", "contact_name", "phone", "email",
            "source", "assigned_to", "stage", "priority", "date_added", "next_follow_up", "remarks",
        ]].copy()
        register.columns = [
            "ID", "Provider", "Type", "Contact", "Phone", "Email", "Source",
            "Current owner", "Current stage", "Priority", "Date added", "Next follow-up", "Remarks",
        ]
        export_button(register, "qurocare-shared-provider-register")
        st.dataframe(register, hide_index=True, width="stretch")

    with st.container(border=True):
        st.subheader("Daily activity calendar")
        selected_day = st.date_input(
            "Select a day to review team activity",
            value=today,
            max_value=today,
            key="daily_activity_date",
        )
        st.caption("This shows whether each scored team member submitted activity on the selected date, with that day’s outcome measure.")
        daily_activity = build_daily_activity_summary(selected_day, dap_today)
        st.dataframe(daily_activity, hide_index=True, width="stretch")


def activity_metrics_for_user(activities: pd.DataFrame, user_id: int) -> tuple[dict, bool]:
    """Add all activity values for one user and report whether they submitted."""
    metrics: dict[str, int] = {}
    if activities.empty:
        return metrics, False
    entries = activities[activities.user_id == user_id]
    for value in entries.metrics_json:
        metrics.update({key: metrics.get(key, 0) + int(number) for key, number in json.loads(value).items()})
    return metrics, not entries.empty


def scorecard_measures(role: str, metrics: dict, dap_today: int) -> list[tuple[str, str, str]]:
    """Return a list of (metric name, measure, evidence) tuples for a role.

    Every formula below uses only that same person's own submitted activity
    fields (each role logs both the numerator and denominator it needs), so
    the numbers are self-contained per person. Formula definitions, as
    agreed with the CEO:

    - Halifa (Market Lead) - Quality lead rate = QIL / M x 100
        QIL = Qualified interested leads added to GMS (her own activity)
        M   = Meaningful provider conversations (her own activity)
    - Rahul (BDM) - Conversion rate = C / QLC x 100
        C   = Converted leads, i.e. leads moved to the Converted stage (his own activity)
        QLC = Qualified leads contacted (his own activity)
    - Dr. Asinsha (MO) - Verification completion rate = V / C x 100
        V = Verifications completed (her own activity)
        C = Converted leads received for verification (her own activity)
    - Reshma (PSM) - Activation-ready rate = AP / V x 100
        AP = Providers ready to activate (her own activity)
        V  = Verified providers ready for onboarding (her own activity)
      Reshma also gets a second measure, DAP coverage = DAP / AP x 100
        DAP = Daily active providers (dashboard placeholder, currently 0
              until Tech connects the provider-app data source)
        AP  = Providers ready to activate (her own activity, same as above)
    """
    if role == ROLE_ML:
        conversations = metrics.get("meaningful_conversations", 0)
        qualified = metrics.get("leads_qualified", 0)
        rate = (qualified / conversations * 100) if conversations else 0
        return [(
            "Quality lead rate",
            f"{rate:.0f}%",
            f"{qualified} qualified leads (QIL) from {conversations} meaningful conversations (M)",
        )]
    if role == ROLE_BDM:
        contacted = metrics.get("qualified_leads_contacted", 0)
        converted = metrics.get("converted_leads", 0)
        rate = (converted / contacted * 100) if contacted else 0
        return [(
            "Conversion rate",
            f"{rate:.0f}%",
            f"{converted} converted leads (C) from {contacted} qualified leads contacted (QLC)",
        )]
    if role == ROLE_MO:
        verified = metrics.get("verifications_completed", 0)
        received = metrics.get("converted_leads_received", 0)
        rate = (verified / received * 100) if received else 0
        return [(
            "Verification completion rate",
            f"{rate:.0f}%",
            f"{verified} verifications completed (V) from {received} converted leads received for verification (C)",
        )]
    if role == ROLE_PSM:
        ap = metrics.get("providers_ready_to_activate", 0)
        v = metrics.get("providers_ready_for_onboarding", 0)
        activation_rate = (ap / v * 100) if v else 0
        dap_rate = (dap_today / ap * 100) if ap else 0
        dap_note = " (DAP is a placeholder until Tech connects the provider-app data source)" if dap_today == 0 else ""
        return [
            (
                "Activation-ready rate",
                f"{activation_rate:.0f}%",
                f"{ap} providers ready to activate (AP) from {v} verified providers ready for onboarding (V)",
            ),
            (
                "DAP coverage",
                f"{dap_rate:.0f}%",
                f"{dap_today} daily active providers (DAP) from {ap} providers ready to activate (AP){dap_note}",
            ),
        ]
    if role == ROLE_CEO:
        return [("Management review", "-", "CEO/Admin view")]
    return []


def build_scorecards(start_date: date, end_date: date, dap_today: int) -> pd.DataFrame:
    users = users_frame()
    # Deactivated accounts remain in the audit trail but are not operational
    # team members and should not appear on the shared scorecard.
    users = users[(users.is_active == 1) & (users.role != ROLE_DISPLAY)]
    role_order = {
        ROLE_ML: 0,
        ROLE_BDM: 1,
        ROLE_MO: 2,
        ROLE_PSM: 3,
        ROLE_CEO: 4,
    }
    users = users.assign(_scorecard_order=users.role.map(role_order).fillna(99))
    users = users.sort_values(["_scorecard_order", "id"])
    activities = frame(
        "SELECT * FROM role_activities WHERE activity_date BETWEEN ? AND ?",
        (start_date.isoformat(), end_date.isoformat()),
    )
    rows = []
    for user in users.itertuples():
        metrics, _ = activity_metrics_for_user(activities, user.id)
        for metric_name, measure, evidence in scorecard_measures(user.role, metrics, dap_today):
            rows.append({"Team member": f"{user.name}, {user.role}", "Metric": metric_name, "Value": measure, "Evidence": evidence})
    return pd.DataFrame(rows)


def build_daily_activity_summary(activity_date: date, dap_today: int) -> pd.DataFrame:
    """Build a shared, read-only daily review for the operational team."""
    users = users_frame()
    users = users[(users.is_active == 1) & (users.role != ROLE_DISPLAY)]
    role_order = {ROLE_ML: 0, ROLE_BDM: 1, ROLE_MO: 2, ROLE_PSM: 3, ROLE_CEO: 4}
    users = users.assign(_daily_order=users.role.map(role_order).fillna(99)).sort_values(["_daily_order", "id"])
    activities = frame(
        "SELECT * FROM role_activities WHERE activity_date=?",
        (activity_date.isoformat(),),
    )
    rows = []
    for user in users.itertuples():
        metrics, submitted = activity_metrics_for_user(activities, user.id)
        if user.role == ROLE_CEO:
            continue
        for metric_name, measure, evidence in scorecard_measures(user.role, metrics, dap_today):
            rows.append(
                {
                    "Team member": f"{user.name}, {user.role}",
                    "Activity submitted": "Yes" if submitted else "No",
                    "Metric": metric_name,
                    "Daily measure": measure,
                    "Evidence": evidence,
                }
            )
    return pd.DataFrame(rows)


def my_leads_page(user: dict) -> None:
    st.title("My provider leads")
    # Halifa (Market Lead) is where Lost providers land for re-engagement, so
    # her queue needs to show them. Every other role's queue hides Lost
    # providers once they've moved on - they stay visible on the Dashboard
    # and in reports regardless.
    include_lost = user["role"] == ROLE_ML
    if include_lost:
        st.caption("Providers marked Lost are routed to you here so you can re-engage them. Move a provider back to Interested if you reconnect - it will route straight back to Rahul.")
    else:
        st.caption("Active provider leads currently assigned to you are listed here. Lost leads are routed to Halifa (Market Lead) for re-engagement and hidden from this list; they remain visible on the Dashboard and in reports.")
    query, params = my_provider_query(user, include_lost=include_lost)
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
            stage = "Interested"
            a.text_input("Current stage", value=stage, disabled=True)
            priority = b.selectbox("Priority", PRIORITIES, index=1)
            followup = c.date_input("Next follow-up", value=date.today())
            notes = st.text_area("Remarks")
            rahul = owner_for_role(ROLE_BDM)
            if rahul:
                st.caption("This qualified lead will be assigned to Rahul automatically.")
            else:
                st.warning("Rahul's active account is not available yet. The lead will remain with you until it is assigned.")
            if st.form_submit_button("Save provider lead", type="primary"):
                if not company.strip():
                    st.error("Provider name is required.")
                else:
                    owner = rahul
                    assigned_to, assigned_to_user_id = owner if owner else (user_label(user), user["id"])
                    execute("""INSERT INTO providers (company_name, provider_type, contact_name, phone, email, source, assigned_to, assigned_to_user_id, created_by_user_id, updated_by_user_id, stage, priority, date_added, next_follow_up, remarks, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""", (company.strip(), provider_type, contact.strip(), phone.strip(), email.strip(), source, assigned_to, assigned_to_user_id, user["id"], user["id"], stage, priority, date.today().isoformat(), followup.isoformat(), notes.strip()))
                    if owner:
                        st.success(f"Provider lead saved. Stage set to {stage}. Assigned to Rahul.")
                    else:
                        st.success(f"Provider lead saved. Stage set to {stage}. Assign it to Rahul after his account is created.")
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
        stage_options = allowed_stages_for_role(user, record.stage)
        stage = a.selectbox("Stage", stage_options, index=stage_options.index(record.stage), key=f"provider_stage_{selected_id}")
        priority = b.selectbox("Priority", PRIORITIES, index=PRIORITIES.index(record.priority), key=f"provider_priority_{selected_id}")
        followup = c.date_input("Next follow-up", value=pd.to_datetime(record.next_follow_up).date() if pd.notna(record.next_follow_up) else date.today(), key=f"provider_followup_{selected_id}")
        notes = st.text_area("Remarks", value=record.remarks or "", key=f"provider_notes_{selected_id}")
        if st.form_submit_button("Save my changes", type="primary"):
            # Ownership only moves automatically at the Verification and
            # Onboarding handoff points (see workflow_owner_for_stage). Every
            # other stage change, including Lost and Active Provider, keeps
            # the provider with its current owner.
            next_owner = workflow_owner_for_stage(stage)
            assigned_to = next_owner[0] if next_owner else record.assigned_to
            # Cast to a native Python int: pandas returns numpy.int64 here,
            # and passing that straight to sqlite3 silently stores it as a
            # BLOB instead of an INTEGER. Once corrupted, the "WHERE
            # assigned_to_user_id = ?" filter used by My provider leads never
            # matches that row again, so the provider vanishes from the
            # owner's list on the very next edit. This was the root cause of
            # providers disappearing before their workflow was finished.
            assigned_to_user_id = next_owner[1] if next_owner else int(record.assigned_to_user_id)
            execute("""UPDATE providers SET company_name=?, provider_type=?, contact_name=?, phone=?, email=?, source=?, assigned_to=?, assigned_to_user_id=?, stage=?, priority=?, next_follow_up=?, remarks=?, last_contact=?, updated_by_user_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""", (company.strip(), provider_type, contact.strip(), phone.strip(), email.strip(), source, assigned_to, assigned_to_user_id, stage, priority, followup.isoformat(), notes.strip(), date.today().isoformat(), user["id"], selected_id))
            if stage != record.stage:
                if next_owner:
                    new_owner_name = next_owner[0].split(",")[0].strip()
                    if stage == "Lost":
                        st.success(f"Stage updated to Lost. Assigned to {new_owner_name} for re-engagement. Hidden from your Provider Leads, but stays visible on the Dashboard and in reports.")
                    else:
                        st.success(f"Stage updated to {stage}. Assigned to {new_owner_name}.")
                elif stage == "Lost":
                    st.success("Stage updated to Lost. Hidden from My provider leads, but stays visible on the Dashboard and in reports.")
                else:
                    st.success(f"Stage updated to {stage}.")
            else:
                st.success("Provider lead updated.")
            st.rerun()

    st.divider()
    st.subheader("Delete provider")
    st.caption("This permanently removes the provider record from the database. This cannot be undone.")
    confirm_delete = st.checkbox(
        f"I confirm I want to permanently delete '{record.company_name}' (#{selected_id}).",
        key=f"confirm_delete_{selected_id}",
    )
    if st.button("Delete provider", type="secondary", disabled=not confirm_delete, key=f"delete_provider_{selected_id}"):
        execute("DELETE FROM providers WHERE id=?", (selected_id,))
        st.success(f"Provider '{record.company_name}' was permanently deleted.")
        st.rerun()


def my_activity_page(user: dict) -> None:
    st.title("My activity")
    fields = ACTIVITY_FIELDS[user["role"]]
    if not fields:
        st.info("This account does not submit operational activity.")
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
        team_options = [f"{name}|{role}" for name, role in DEFAULT_TEAM] + [
            f"CEO|{ROLE_CEO}",
            f"LG display|{ROLE_DISPLAY}",
        ]
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
    accounts = users_frame()
    other_accounts = accounts[accounts.id != user["id"]]
    if not other_accounts.empty:
        st.divider()
        st.subheader("Deactivate account")
        st.caption("Deactivated accounts cannot sign in and will not appear on the team scorecard. Their past records are retained.")
        account_options = other_accounts.id.tolist()
        selected_account_id = st.selectbox(
            "Select account to deactivate",
            account_options,
            format_func=lambda account_id: (
                f"{other_accounts[other_accounts.id == account_id].iloc[0]['name']} — "
                f"{other_accounts[other_accounts.id == account_id].iloc[0]['email']}"
            ),
        )
        selected_account = other_accounts[other_accounts.id == selected_account_id].iloc[0]
        if selected_account.is_active:
            if st.button("Deactivate selected account", type="secondary"):
                execute("UPDATE users SET is_active=0 WHERE id=?", (selected_account_id,))
                st.success(f"{selected_account['name']} has been deactivated.")
                st.rerun()
        else:
            st.info("This account is already deactivated.")
    st.subheader("Current accounts")
    accounts["access_status"] = accounts.is_active.map({1: "Active", 0: "Deactivated"})
    st.dataframe(accounts.drop(columns=["id", "is_active"]), hide_index=True, width="stretch")


def main_app() -> None:
    user = current_user()
    with st.sidebar:
        st.title("Qurocare GMS")
        st.caption(user_label(user))
        if st.button("Sign out", icon=":material/logout:"):
            st.session_state["gms_user"] = None
            st.rerun()
        st.divider()
        if user["role"] == ROLE_DISPLAY:
            pages = ["Dashboard"]
        elif user["role"] == ROLE_PGA:
            pages = ["Dashboard", "My provider leads"]
        else:
            pages = ["Dashboard", "My provider leads", "My activity", "My provider feedback"]
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
    elif page == "Team access":
        user_management_page(user)


if user_count() == 0:
    setup_first_admin()
elif current_user() is None:
    login_page()
else:
    main_app()
