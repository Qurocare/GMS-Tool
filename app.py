from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from db import (
    FEEDBACK_CATEGORIES, FEEDBACK_STATUSES, PRIORITIES, PROVIDER_TYPES,
    ROLE_BDM, ROLE_CEO, ROLE_DISPLAY, ROLE_ML, ROLE_MO, ROLE_PGA, ROLE_PSM, SOURCES, STAGES,
    SUB_STAGE_DEMO_NOT_SCHEDULING, SUB_STAGE_DEMO_SCHEDULING, SUB_STAGE_ONBOARDING_REQUESTED,
    SUB_STAGE_VERIFICATION_REQUESTED, authenticate, count_stage_events, create_user, execute,
    export_all_data, frame, import_all_data, initialise, log_stage_change, stage_log_for_user,
    user_count, users_frame,
)

st.set_page_config(page_title="Qurocare GMS", page_icon=":material/health_and_safety:", layout="wide")
initialise()
st.session_state.setdefault("gms_user", None)

# Manual daily entry, by role. Everything else (every stage move and every
# sub-stage checkbox) is logged automatically to stage_history the moment it
# happens, and the KPIs are computed from that log - nobody types in a
# converted/verified/qualified count by hand any more.
ACTIVITY_FIELDS = {
    ROLE_ML: [
        ("researches", "Researches"),
        ("calls", "Calls"),
        ("demos_supported", "Demo supported"),
    ],
    ROLE_BDM: [
        ("researches", "Researches"),
        ("calls", "Calls"),
    ],
    ROLE_PSM: [
        ("researches", "Researches"),
        ("calls", "Calls"),
        ("demos_supported", "Demo supported"),
    ],
    ROLE_MO: [
        ("researches", "Researches"),
        ("calls", "Calls"),
        ("demos_supported", "Demo supported"),
    ],
    ROLE_PGA: [],
    ROLE_CEO: [],
}


def current_user() -> dict:
    return st.session_state["gms_user"]


def is_management(user: dict) -> bool:
    return user["role"] in {ROLE_PSM, ROLE_CEO}


def owner_for_role(role: str) -> tuple[str, int] | None:
    """Return the active GMS account responsible for a workflow role.

    The label returned is the role itself, not the person's name - names are
    not shown anywhere in the operational UI. The account's real identity is
    still tracked via the id (and stage_history.changed_by_user_id) for
    anyone who needs to audit who actually did what.
    """
    users = frame(
        "SELECT id, role FROM users WHERE role=? AND is_active=1 ORDER BY id LIMIT 1",
        (role,),
    )
    if users.empty:
        return None
    owner = users.iloc[0]
    return owner["role"], int(owner["id"])


def resolve_ownership(stage: str, demo_choice: str | None, onboarding_requested: bool, verification_requested: bool) -> tuple[tuple[str, int] | None, str]:
    """Return (next_owner_or_None, sub_stage_text) for a stage change.

    Ownership moves automatically at these points:
    - Provider Qualified -> Provider Partnerships, whichever of the two demo
      sub-stages is chosen (both route the same way; Partnerships handles
      getting the demo actually scheduled either way).
    - Converted/ACTIVE Provider -> Provider Success by default. If only
      "Request for Verification" is checked (no Onboarding), it goes
      straight to Provider Verification instead, skipping Success entirely -
      the self-onboarded case. If Onboarding is checked (with or without
      Verification), it goes to Provider Success either way, since Success
      has to do the onboarding work first; a pre-checked Verification
      request just carries forward as the sub-stage.
    - Provider Active Onboarded / Not-Onboarded -> stays with Provider
      Success, unless "Request for Verification" is checked, which sends it
      to Provider Verification.
    - Provider Verified / Provider Not Verified -> Provider Success either
      way, for ongoing DAP monitoring.
    - Lost -> Market Intelligence, from wherever it's marked (only Market
      Intelligence and Provider Partnerships can mark a provider Lost).
    Every other stage carries no ownership change.
    """
    if stage == "Provider Qualified":
        return owner_for_role(ROLE_BDM), (demo_choice or "")
    if stage == "Converted/ACTIVE Provider":
        flags = []
        if onboarding_requested:
            flags.append(SUB_STAGE_ONBOARDING_REQUESTED)
        if verification_requested:
            flags.append(SUB_STAGE_VERIFICATION_REQUESTED)
        sub_stage = ", ".join(flags)
        if onboarding_requested:
            return owner_for_role(ROLE_PSM), sub_stage
        if verification_requested:
            return owner_for_role(ROLE_MO), sub_stage
        return owner_for_role(ROLE_PSM), sub_stage
    if stage in ("Provider Active Onboarded (PAO)", "Provider Active Not-Onboarded (PANO)"):
        sub_stage = SUB_STAGE_VERIFICATION_REQUESTED if verification_requested else ""
        if verification_requested:
            return owner_for_role(ROLE_MO), sub_stage
        return None, sub_stage
    if stage in ("Provider Verified", "Provider Not Verified"):
        return owner_for_role(ROLE_PSM), ""
    if stage == "Lost":
        return owner_for_role(ROLE_ML), ""
    return None, ""


def allowed_stages_for_role(user: dict, current_stage: str) -> list[str]:
    """Limit workflow changes to the stages owned by the signed-in role."""
    allowed_by_role = {
        ROLE_ML: ["Provider Identified", "Provider Qualified", "Lost"],
        ROLE_BDM: ["Contacted for Demo Scheduling", "Demo Scheduled", "Demo Completed", "Converted/ACTIVE Provider", "Lost"],
        ROLE_PSM: ["Provider Active Onboarded (PAO)", "Provider Active Not-Onboarded (PANO)"],
        ROLE_MO: ["Provider Verified", "Provider Not Verified"],
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
    st.info("Create the first local pilot account for the Provider Success role. Before web deployment, this local login will be replaced by Supabase Auth.")
    with st.form("first_admin"):
        name = st.text_input("Your name (for internal records only - not shown elsewhere in the app)")
        email = st.text_input("Work email")
        password = st.text_input("Create password", type="password")
        confirm = st.text_input("Confirm password", type="password")
        if st.form_submit_button("Create first account", type="primary"):
            if not name.strip() or not email.strip() or len(password) < 8:
                st.error("Enter your name, a work email, and a password of at least 8 characters.")
            elif password != confirm:
                st.error("Passwords do not match.")
            else:
                create_user(name.strip(), ROLE_PSM, email, password)
                first_user = authenticate(email, password)
                # Preserve existing pilot records by making the first Provider
                # Success account their accountable owner. Future records use
                # the logged-in user.
                execute("UPDATE providers SET created_by_user_id=?, assigned_to_user_id=?, assigned_to=?, updated_by_user_id=? WHERE created_by_user_id IS NULL AND assigned_to_user_id IS NULL", (first_user["id"], first_user["id"], first_user["role"], first_user["id"]))
                execute("UPDATE feedback SET submitted_by_user_id=?, updated_by_user_id=? WHERE submitted_by_user_id IS NULL", (first_user["id"], first_user["id"]))
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
    demos = int(providers.stage.isin(["Demo Scheduled", "Demo Completed"]).sum()) if total else 0
    converted_active = int((providers.stage == "Converted/ACTIVE Provider").sum()) if total else 0
    pao = int((providers.stage == "Provider Active Onboarded (PAO)").sum()) if total else 0
    pano = int((providers.stage == "Provider Active Not-Onboarded (PANO)").sum()) if total else 0
    ap_total = active_provider_count()
    due_dates = pd.to_datetime(providers.get("next_follow_up"), errors="coerce") if total else pd.Series(dtype="datetime64[ns]")
    due = int((due_dates.dt.date <= date.today()).sum()) if not due_dates.empty else 0
    with st.container(horizontal=True):
        st.metric("Total leads", total, border=True)
        st.metric("Demos", demos, border=True, help="Demo Scheduled + Demo Completed. A completed demo does not by itself mean the lead converted.")
        st.metric("Converted/ACTIVE", converted_active, border=True, help="Providers currently sitting at Converted/ACTIVE Provider, not yet moved into PAO/PANO or Verification.")
        st.metric("PAO", pao, border=True, help="Provider Active Onboarded - Provider Success personally helped onboard these.")
        st.metric("PANO", pano, border=True, help="Provider Active Not-Onboarded - active, but self-onboarded without Provider Success's help.")
        st.metric("Active providers (AP)", ap_total, border=True, help="Running total of every provider at Converted/ACTIVE or later, regardless of period selected below.")
        st.metric("Daily active providers (DAP)", dap_today, border=True,
                  help="Currently shown as 0 until the provider-app data integration is available.")
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
    st.caption(f"KPIs below are computed from stage changes made from {start_date.strftime('%d %b %Y')} to {end_date.strftime('%d %b %Y')}.")

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
            scorecards = build_scorecards(start_date, end_date, dap_today, ap_total)
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
            "id", "company_name", "provider_type", "contact_name", "phone", "org_contact_number", "email",
            "source", "assigned_to", "stage", "sub_stage", "priority", "date_added", "next_follow_up", "remarks",
        ]].copy()
        register.columns = [
            "ID", "Provider", "Type", "Contact", "Contact phone", "Org phone", "Email", "Source",
            "Current owner", "Current stage", "Sub-stage", "Priority", "Date added", "Next follow-up", "Remarks",
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
        st.caption("This shows whether each role submitted its manual daily entry (Researches / Calls / Demo supported) on the selected date.")
        daily_activity = build_daily_activity_summary(selected_day)
        st.dataframe(daily_activity, hide_index=True, width="stretch")


def activity_metrics_for_user(activities: pd.DataFrame, user_id: int) -> tuple[dict, bool]:
    """Add all manually-entered activity values for one user and report
    whether they submitted anything for the period (Researches, Calls, Demo
    supported - everything else is derived automatically, see role_kpi)."""
    metrics: dict[str, int] = {}
    if activities.empty:
        return metrics, False
    entries = activities[activities.user_id == user_id]
    for value in entries.metrics_json:
        metrics.update({key: metrics.get(key, 0) + int(number) for key, number in json.loads(value).items()})
    return metrics, not entries.empty


def active_provider_count() -> int:
    """AP: providers currently at Converted/ACTIVE Provider or any stage
    downstream of it. A running total, not scoped to a date range."""
    active_stages = (
        "Converted/ACTIVE Provider", "Provider Active Onboarded (PAO)",
        "Provider Active Not-Onboarded (PANO)", "Provider Verified", "Provider Not Verified",
    )
    placeholders = ",".join("?" for _ in active_stages)
    result = frame(f"SELECT COUNT(*) AS c FROM providers WHERE stage IN ({placeholders})", active_stages)
    return int(result.iloc[0].c) if not result.empty else 0


def role_kpi(role: str, start_date: date, end_date: date, dap_today: int, ap_total: int) -> list[tuple[str, str, str]]:
    """Return a list of (KPI short name, value, evidence) for a role.

    Every number here comes from stage_history for the selected period - not
    from anyone's manually typed activity - so this is one shared, team-wide
    figure per role rather than a per-person number. Formula definitions, as
    finalized:

    - Market Intelligence - LQR = Q / M x 100
    - Provider Partnerships - LCR = C / Q x 100
    - Provider Success - OSR = O / RO x 100, and DAP Coverage = DAP / AP x 100
        (AP here is the running total of all active providers, not scoped
        to the period; DAP is the dashboard's daily-active-providers figure)
    - Provider Verification - VCR = V / RV x 100
    """
    if role == ROLE_ML:
        m = count_stage_events(start_date, end_date, to_stage="Provider Identified")
        q = count_stage_events(start_date, end_date, to_stage="Provider Qualified")
        rate = (q / m * 100) if m else 0
        return [("LQR", f"{rate:.0f}%", f"{q} Qualified (Q) from {m} Identified/Meaningful conversations (M)")]
    if role == ROLE_BDM:
        q = count_stage_events(start_date, end_date, to_stage="Provider Qualified")
        c = count_stage_events(start_date, end_date, to_stage="Converted/ACTIVE Provider")
        rate = (c / q * 100) if q else 0
        return [("LCR", f"{rate:.0f}%", f"{c} Converted (C) from {q} Qualified (Q)")]
    if role == ROLE_MO:
        rv = count_stage_events(start_date, end_date, sub_stage_contains=SUB_STAGE_VERIFICATION_REQUESTED)
        v = count_stage_events(start_date, end_date, to_stage="Provider Verified")
        rate = (v / rv * 100) if rv else 0
        return [("VCR", f"{rate:.0f}%", f"{v} Verified (V) from {rv} Requests for Verification (RV)")]
    if role == ROLE_PSM:
        ro = count_stage_events(start_date, end_date, sub_stage_contains=SUB_STAGE_ONBOARDING_REQUESTED)
        o = count_stage_events(start_date, end_date, to_stage="Provider Active Onboarded (PAO)")
        osr = (o / ro * 100) if ro else 0
        dap_rate = (dap_today / ap_total * 100) if ap_total else 0
        dap_note = " (DAP is a placeholder until Tech connects the provider-app data source)" if dap_today == 0 else ""
        return [
            ("OSR", f"{osr:.0f}%", f"{o} Onboarded (O) from {ro} Requests for Onboarding (RO)"),
            ("DAP Coverage", f"{dap_rate:.0f}%", f"{dap_today} Daily Active Providers (DAP) from {ap_total} Active Providers (AP), total{dap_note}"),
        ]
    if role == ROLE_CEO:
        return [("Management review", "-", "CEO/Admin view")]
    return []


def build_scorecards(start_date: date, end_date: date, dap_today: int, ap_total: int) -> pd.DataFrame:
    """One row per role per KPI - team-wide, not per person, since the
    underlying counts come from everyone's stage changes in that role."""
    rows = []
    for role in (ROLE_ML, ROLE_BDM, ROLE_MO, ROLE_PSM, ROLE_CEO):
        for metric_name, value, evidence in role_kpi(role, start_date, end_date, dap_today, ap_total):
            rows.append({"Role": role, "KPI": metric_name, "Value": value, "Evidence": evidence})
    return pd.DataFrame(rows)


def build_daily_activity_summary(activity_date: date) -> pd.DataFrame:
    """Shared, read-only view of who submitted their manual daily entry
    (Researches / Calls / Demo supported) on a given date."""
    users = users_frame()
    users = users[(users.is_active == 1) & (users.role != ROLE_DISPLAY) & (users.role != ROLE_CEO)]
    role_order = {ROLE_ML: 0, ROLE_BDM: 1, ROLE_MO: 2, ROLE_PSM: 3}
    users = users.assign(_daily_order=users.role.map(role_order).fillna(99)).sort_values(["_daily_order", "id"])
    activities = frame(
        "SELECT * FROM role_activities WHERE activity_date=?",
        (activity_date.isoformat(),),
    )
    rows = []
    for user in users.itertuples():
        metrics, submitted = activity_metrics_for_user(activities, user.id)
        rows.append(
            {
                "Role": user.role,
                "Activity submitted": "Yes" if submitted else "No",
                "Researches": metrics.get("researches", 0),
                "Calls": metrics.get("calls", 0),
                "Demo supported": metrics.get("demos_supported", 0),
            }
        )
    return pd.DataFrame(rows)


def my_leads_page(user: dict) -> None:
    st.title("My provider leads")
    # Market Intelligence is where Lost providers land for re-engagement, so
    # their queue needs to show them. Every other role's queue hides Lost
    # providers once they've moved on - they stay visible on the Dashboard
    # and in reports regardless.
    include_lost = user["role"] == ROLE_ML
    if include_lost:
        st.caption("Providers marked Lost are routed to you here so you can re-engage them. Move a provider back to Provider Qualified if you reconnect - it will route straight back to Provider Partnerships.")
    else:
        st.caption("Active provider leads currently assigned to you are listed here. Lost leads are routed to Market Intelligence for re-engagement and hidden from this list; they remain visible on the Dashboard and in reports.")
    query, params = my_provider_query(user, include_lost=include_lost)
    providers = frame(query, params)

    if user["role"] in (ROLE_ML, ROLE_CEO):
        with st.expander("Add provider lead", expanded=False):
            with st.form("new_provider", clear_on_submit=True):
                a, b, c = st.columns(3)
                company = a.text_input("Provider / organisation name *")
                provider_type = choose_or_specify("Provider type *", PROVIDER_TYPES, "new_type")
                contact = c.text_input("Contact person")
                a, b, c = st.columns(3)
                phone = a.text_input("Contact person's phone")
                org_phone = b.text_input("Organization / individual contact number")
                email = c.text_input("Email")
                a, b, c = st.columns(3)
                source = choose_or_specify("Lead source", SOURCES, "new_source")
                priority = b.selectbox("Priority", PRIORITIES, index=1)
                followup = c.date_input("Next follow-up", value=date.today())
                stage = "Provider Identified"
                st.text_input("Current stage", value=stage, disabled=True)
                notes = st.text_area("Remarks")
                st.caption("This is logged as a Provider Identified / Meaningful Conversation (M) and stays with you until you mark it Qualified.")
                if st.form_submit_button("Save provider lead", type="primary"):
                    if not company.strip():
                        st.error("Provider name is required.")
                    else:
                        assigned_to, assigned_to_user_id = user["role"], user["id"]
                        execute("""INSERT INTO providers (company_name, provider_type, contact_name, phone, org_contact_number, email, source, assigned_to, assigned_to_user_id, created_by_user_id, updated_by_user_id, stage, priority, date_added, next_follow_up, remarks, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""", (company.strip(), provider_type, contact.strip(), phone.strip(), org_phone.strip(), email.strip(), source, assigned_to, assigned_to_user_id, user["id"], user["id"], stage, priority, date.today().isoformat(), followup.isoformat(), notes.strip()))
                        new_id = frame("SELECT id FROM providers WHERE company_name=? ORDER BY id DESC LIMIT 1", (company.strip(),)).iloc[0].id
                        log_stage_change(int(new_id), None, stage, "", user)
                        st.success(f"Provider lead saved. Stage set to {stage}.")
                        st.rerun()

    st.subheader("My lead register")
    if providers.empty:
        st.info("No leads are assigned to you yet.")
        return
    export_button(providers, "my-provider-leads")
    show = providers[["id", "company_name", "provider_type", "contact_name", "phone", "org_contact_number", "source", "stage", "sub_stage", "priority", "next_follow_up", "remarks"]].copy()
    show.columns = ["ID", "Provider", "Type", "Contact", "Contact phone", "Org phone", "Source", "Stage", "Sub-stage", "Priority", "Follow-up", "Remarks"]
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
        phone = a.text_input("Contact person's phone", value=record.phone or "", key=f"provider_phone_{selected_id}")
        org_phone = b.text_input("Organization / individual contact number", value=record.org_contact_number or "", key=f"provider_org_phone_{selected_id}")
        email = c.text_input("Email", value=record.email or "", key=f"provider_email_{selected_id}")
        source = choose_or_specify("Lead source", SOURCES, f"provider_source_{selected_id}", record.source)
        a, b, c = st.columns(3)
        stage_options = allowed_stages_for_role(user, record.stage)
        stage = a.selectbox("Stage", stage_options, index=stage_options.index(record.stage), key=f"provider_stage_{selected_id}")
        priority = b.selectbox("Priority", PRIORITIES, index=PRIORITIES.index(record.priority), key=f"provider_priority_{selected_id}")
        followup = c.date_input("Next follow-up", value=pd.to_datetime(record.next_follow_up).date() if pd.notna(record.next_follow_up) else date.today(), key=f"provider_followup_{selected_id}")
        notes = st.text_area("Remarks", value=record.remarks or "", key=f"provider_notes_{selected_id}")

        # Sub-stage checkboxes: shown per role since Streamlit forms can't
        # dynamically show/hide fields based on another field's live value
        # (nothing reruns until the whole form submits). Each is only
        # actually applied if the stage you submit matches where it belongs.
        demo_choice = None
        onboarding_requested = False
        verification_requested = False
        if user["role"] in (ROLE_ML, ROLE_CEO):
            st.caption("Only used if you set the stage above to Provider Qualified:")
            demo_choice = st.radio(
                "Demo scheduling", [SUB_STAGE_DEMO_SCHEDULING, SUB_STAGE_DEMO_NOT_SCHEDULING],
                key=f"demo_choice_{selected_id}", horizontal=True,
            )
        if user["role"] in (ROLE_BDM, ROLE_CEO):
            st.caption("Only used if you set the stage above to Converted/ACTIVE Provider:")
            d, e = st.columns(2)
            onboarding_requested = d.checkbox("Request for Onboarding", key=f"onboarding_req_{selected_id}")
            verification_requested_pp = e.checkbox("Request for Verification", key=f"verification_req_pp_{selected_id}")
            verification_requested = verification_requested or verification_requested_pp
        if user["role"] in (ROLE_PSM, ROLE_CEO):
            st.caption("Only used if you set the stage above to Provider Active Onboarded/Not-Onboarded (PAO/PANO):")
            verification_requested_ps = st.checkbox("Request for Verification", key=f"verification_req_ps_{selected_id}")
            verification_requested = verification_requested or verification_requested_ps

        if st.form_submit_button("Save my changes", type="primary"):
            next_owner, sub_stage = resolve_ownership(stage, demo_choice, onboarding_requested, verification_requested)
            assigned_to = next_owner[0] if next_owner else record.assigned_to
            # Cast to a native Python int: pandas returns numpy.int64 here,
            # and passing that straight to sqlite3 silently stores it as a
            # BLOB instead of an INTEGER. Once corrupted, the "WHERE
            # assigned_to_user_id = ?" filter used by My provider leads never
            # matches that row again, so the provider vanishes from the
            # owner's list on the very next edit. This was the root cause of
            # providers disappearing before their workflow was finished.
            assigned_to_user_id = next_owner[1] if next_owner else int(record.assigned_to_user_id)
            execute("""UPDATE providers SET company_name=?, provider_type=?, contact_name=?, phone=?, org_contact_number=?, email=?, source=?, assigned_to=?, assigned_to_user_id=?, stage=?, sub_stage=?, priority=?, next_follow_up=?, remarks=?, last_contact=?, updated_by_user_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""", (company.strip(), provider_type, contact.strip(), phone.strip(), org_phone.strip(), email.strip(), source, assigned_to, assigned_to_user_id, stage, sub_stage, priority, followup.isoformat(), notes.strip(), date.today().isoformat(), user["id"], selected_id))
            stage_changed = stage != record.stage
            sub_stage_changed = sub_stage != (record.sub_stage or "")
            if stage_changed or sub_stage_changed:
                log_stage_change(int(selected_id), record.stage, stage, sub_stage, user)
            if stage_changed:
                if next_owner:
                    if stage == "Lost":
                        st.success(f"Stage updated to Lost. Assigned to {next_owner[0]} for re-engagement. Hidden from your Provider Leads, but stays visible on the Dashboard and in reports.")
                    else:
                        st.success(f"Stage updated to {stage}. Assigned to {next_owner[0]}.")
                else:
                    st.success(f"Stage updated to {stage}.")
            elif sub_stage_changed:
                st.success(f"Sub-stage updated to: {sub_stage or '(cleared)'}.")
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
        # Also remove this provider's stage-change history - otherwise its
        # old log entries keep silently counting toward the KPIs (LQR, LCR,
        # OSR, VCR) forever, even though the provider itself is gone.
        execute("DELETE FROM stage_history WHERE provider_id=?", (selected_id,))
        execute("DELETE FROM providers WHERE id=?", (selected_id,))
        st.success(f"Provider '{record.company_name}' was permanently deleted.")
        st.rerun()


def my_activity_page(user: dict) -> None:
    st.title("My activity")
    fields = ACTIVITY_FIELDS[user["role"]]
    if not fields:
        st.info("This account does not submit operational activity.")
        return
    st.caption(f"Your activity is saved under your account ({user['role']}). Only you can edit these entries.")

    with st.form("new_activity", clear_on_submit=True):
        st.caption("Manually entered")
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

    st.divider()
    st.subheader("Review my activity for a date")
    review_date = st.date_input("Select a date to review", value=date.today(), max_value=date.today(), key="activity_review_date")

    st.markdown("**Manually entered on this date**")
    manual_today = frame("SELECT metrics_json, notes FROM role_activities WHERE user_id=? AND activity_date=? ORDER BY id DESC", (user["id"], review_date.isoformat()))
    if manual_today.empty:
        st.caption("No manual entry submitted for this date.")
    else:
        for _, row in manual_today.iterrows():
            metrics = json.loads(row.metrics_json)
            st.write(" · ".join(f"{label}: {metrics.get(key, 0)}" for key, label in fields))
            if row.notes:
                st.caption(f"Notes: {row.notes}")

    st.markdown("**Automatically logged on this date** (from your stage changes)")
    log = stage_log_for_user(user["id"], review_date, review_date)
    if log.empty:
        st.caption("No stage changes logged for this date.")
    else:
        # Rollup counts: how many times each stage was reached, and each
        # sub-stage flag was set, on this date - so the numbers don't have
        # to be counted by hand from the detailed rows below.
        stage_counts = log.to_stage.value_counts()
        sub_stage_counts: dict[str, int] = {}
        for value in log.sub_stage.dropna():
            for flag in [f.strip() for f in value.split(",") if f.strip()]:
                sub_stage_counts[flag] = sub_stage_counts.get(flag, 0) + 1
        rollup = [f"{stage}: {count}" for stage, count in stage_counts.items()]
        rollup += [f"{flag}: {count}" for flag, count in sub_stage_counts.items()]
        st.caption(" · ".join(rollup))

        display_log = log.copy()
        display_log.columns = ["Time", "Organization", "From stage", "To stage", "Sub-stage"]
        st.dataframe(display_log, hide_index=True, width="stretch")

    st.divider()
    st.subheader("Full activity log")
    st.caption("Every stage change you've made, across all time.")
    full_log = stage_log_for_user(user["id"])
    if full_log.empty:
        st.info("No stage changes logged yet.")
    else:
        full_log_display = full_log.copy()
        full_log_display.columns = ["Time", "Organization", "From stage", "To stage", "Sub-stage"]
        export_button(full_log_display, "my-stage-activity-log")
        st.dataframe(full_log_display, hide_index=True, width="stretch")

    st.subheader("My manual activity history")
    data = frame("SELECT id, activity_date, metrics_json, notes FROM role_activities WHERE user_id=? ORDER BY activity_date DESC, id DESC", (user["id"],))
    if data.empty:
        st.info("No manual activity submitted yet.")
        return
    display = data.copy()
    for key, label in fields:
        display[label] = display.metrics_json.apply(lambda value: json.loads(value).get(key, 0))
    display = display.drop(columns=["id", "metrics_json"])
    export_button(display, "my-manual-activity")
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
        st.caption(f"Submitted by: {user['role']}")
        if st.form_submit_button("Save my feedback", type="primary"):
            if not description.strip():
                st.error("Feedback description is required.")
            else:
                execute("""INSERT INTO feedback (provider_name, submitted_by, submitted_by_user_id, feedback_date, category, priority, description, assigned_to, status, release_version, updated_by_user_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'New', '', ?, CURRENT_TIMESTAMP)""", (provider, user["role"], user["id"], date.today().isoformat(), category, priority, description.strip(), tech_owner.strip(), user["id"]))
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


def user_management_page(user: dict) -> None:
    st.title("Team access")
    st.caption("Only Provider Success and CEO/Admin can create local pilot accounts. Each person keeps their own individual login; only their role is shown elsewhere in the app.")
    with st.form("new_user", clear_on_submit=True):
        role_options = [ROLE_ML, ROLE_BDM, ROLE_MO, ROLE_PSM, ROLE_PGA, ROLE_CEO, ROLE_DISPLAY]
        role = st.selectbox("Role", role_options)
        name = st.text_input("Person's name (for internal records only - not shown elsewhere in the app)")
        email = st.text_input("Work email")
        password = st.text_input("Temporary password", type="password")
        if st.form_submit_button("Create account", type="primary"):
            if not name.strip() or not email.strip() or len(password) < 8:
                st.error("Enter their name, a work email, and a temporary password of at least 8 characters.")
            else:
                try:
                    create_user(name.strip(), role, email, password)
                    st.success(f"Account created for the {role} role.")
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
                st.success("The account has been deactivated.")
                st.rerun()
        else:
            st.info("This account is already deactivated.")
    st.subheader("Current accounts")
    accounts["access_status"] = accounts.is_active.map({1: "Active", 0: "Deactivated"})
    st.dataframe(accounts.drop(columns=["id", "is_active"]), hide_index=True, width="stretch")

    st.divider()
    st.subheader("Backup & restore")
    st.caption(
        "Streamlit Community Cloud does not guarantee this app's local storage persists - the "
        "platform can reset it at any time, wiping every account and record. Until this moves to "
        "a permanent database, download a backup regularly (e.g. once a day) so a reset costs you "
        "a quick restore instead of rebuilding everything from scratch."
    )
    backup_json = json.dumps(export_all_data(), indent=2, default=str)
    st.download_button(
        "Download full backup (JSON)",
        data=backup_json,
        file_name=f"qurocare-gms-backup-{date.today().isoformat()}.json",
        mime="application/json",
        type="primary",
    )

    st.markdown("**Restore from a backup**")
    st.warning("This permanently replaces every account and record currently in the app with whatever is in the uploaded file. Only use this right after a reset, to reload your last backup.")
    uploaded = st.file_uploader("Choose a backup file", type="json", key="restore_backup_uploader")
    if uploaded is not None:
        try:
            backup_data = json.loads(uploaded.read())
        except Exception:
            st.error("This file doesn't look like a valid backup (couldn't be read as JSON).")
        else:
            confirm_restore = st.checkbox("I understand this will erase everything currently in the app and replace it with this backup.")
            if st.button("Restore this backup", type="secondary", disabled=not confirm_restore):
                import_all_data(backup_data)
                st.success("Backup restored. Please sign in again.")
                st.session_state["gms_user"] = None
                st.rerun()


def tv_dashboard_page(user: dict) -> None:
    """Portrait-native summary for the LG TV display: DAP, AP, and the four
    core KPIs, computed for yesterday specifically (a completed day's report
    card, reviewed the next day - not "today so far," which would read 0%
    for most of every morning) - no date-range picker, no admin detail. A
    separate view from the full Dashboard so it can use much larger type,
    sized for reading across a room, and fill a portrait screen without the
    sidebar/navigation taking up space.
    """
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"] { display: none; }
        [data-testid="stAppViewContainer"] .main .block-container {
            padding-top: 2rem; padding-bottom: 2rem; padding-left: 1.5rem; padding-right: 1.5rem;
            max-width: 100%;
        }
        [data-testid="stAppViewContainer"] { background: #0b0f19; }
        .tv-header { text-align: center; color: #f5f7fa; font-size: 2.6rem; font-weight: 800; margin-bottom: 0.1rem; }
        .tv-subheader { text-align: center; color: #9aa4b2; font-size: 1.4rem; margin-bottom: 2rem; }
        .tv-bignum-row { display: flex; justify-content: center; gap: 2.5rem; margin-bottom: 2.5rem; flex-wrap: wrap; }
        .tv-bignum { text-align: center; }
        .tv-bignum .value { font-size: 5rem; font-weight: 800; color: #f5f7fa; line-height: 1; }
        .tv-bignum .label { font-size: 1.3rem; color: #9aa4b2; margin-top: 0.5rem; }
        .tv-kpi-card { background: #161b26; border-radius: 18px; padding: 1.6rem 1.8rem; margin-bottom: 1.25rem; }
        .tv-kpi-role { font-size: 1.4rem; color: #9aa4b2; margin-bottom: 0.25rem; }
        .tv-kpi-row { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 0.5rem; }
        .tv-kpi-name { font-size: 1.8rem; font-weight: 700; color: #f5f7fa; }
        .tv-kpi-value { font-size: 3.2rem; font-weight: 800; color: #4fd1c5; }
        .tv-kpi-evidence { font-size: 1.05rem; color: #6b7684; margin-top: 0.4rem; }
        .tv-signout { text-align: center; margin-top: 1.5rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # The manager reviews this as "how did yesterday go," not "how's today
    # going so far" - today's numbers would mostly read 0% for the first
    # half of every day until people log activity, which isn't a useful
    # thing to have on permanent display. AP (a running total) still reflects
    # right now regardless of which day the KPIs are reporting on.
    report_date = date.today() - timedelta(days=1)
    dap_today = 0  # placeholder until Tech connects the provider-app data source
    ap_total = active_provider_count()

    st.markdown('<div class="tv-header">Qurocare Growth</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="tv-subheader">Yesterday\'s performance &middot; {report_date.strftime("%A, %d %B %Y")}</div>', unsafe_allow_html=True)
    st.markdown(
        f"""
        <div class="tv-bignum-row">
          <div class="tv-bignum"><div class="value">{dap_today}</div><div class="label">Daily Active<br>Providers</div></div>
          <div class="tv-bignum"><div class="value">{ap_total}</div><div class="label">Active<br>Providers</div></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    for role in (ROLE_ML, ROLE_BDM, ROLE_MO, ROLE_PSM):
        for metric_name, value, evidence in role_kpi(role, report_date, report_date, dap_today, ap_total):
            st.markdown(
                f"""
                <div class="tv-kpi-card">
                  <div class="tv-kpi-role">{role}</div>
                  <div class="tv-kpi-row">
                    <div class="tv-kpi-name">{metric_name}</div>
                    <div class="tv-kpi-value">{value}</div>
                  </div>
                  <div class="tv-kpi-evidence">{evidence}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.markdown('<div class="tv-signout">', unsafe_allow_html=True)
    if st.button("Sign out", icon=":material/logout:"):
        st.session_state["gms_user"] = None
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)


def main_app() -> None:
    user = current_user()
    if user["role"] == ROLE_DISPLAY:
        # The Display account only ever shows the TV view, full-page, with
        # no sidebar or navigation - it's meant to be captured and shown on
        # the LG signage screen, not browsed interactively.
        tv_dashboard_page(user)
        return
    with st.sidebar:
        st.title("Qurocare GMS")
        st.caption(user["role"])
        if st.button("Sign out", icon=":material/logout:"):
            st.session_state["gms_user"] = None
            st.rerun()
        st.divider()
        if user["role"] == ROLE_PGA:
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
