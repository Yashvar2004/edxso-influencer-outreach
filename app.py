"""Streamlit entry point for the outreach data workflow."""

import csv
import json

import streamlit as st
import pandas as pd

from outreach.config import Settings
from outreach.database import (
    DatabaseError,
    count_influencers,
    connect,
    initialize_database,
)
from outreach.discovery import CSVDiscoveryProvider, DiscoveryRequest, import_batch
from outreach.discovery.template import generate_csv_template
from outreach.discovery.youtube_api import YouTubeAPIError, YouTubeAPIProvider
from outreach.qualification.rules import QualificationRules
from outreach.qualification.service import (
    email_readiness_for, evaluate_all, evaluation_history, get_evidence,
    list_influencers,
)
from outreach.personalization.factory import create_provider
from outreach.personalization.models import GenerationError, ProviderError
from outreach.personalization.generator import count_words
from outreach.personalization.service import (
    approve_current_message, edit_current_message, generate_and_save,
    list_current_messages, message_history,
)
from outreach.sending import (
    OutreachNotReady, list_outreach_activity, list_send_candidates,
    simulate_email_send,
)


def dataset_export(database_path: str) -> bytes:
    """Build the submission dataset with per-field verification sources."""
    records = list_influencers(database_path)
    with connect(database_path) as connection:
        provenance_rows = connection.execute(
            "SELECT influencer_id,field_name,verification_status,source_url FROM influencer_field_provenance"
        ).fetchall()
    provenance = {(int(row["influencer_id"]), str(row["field_name"])): row for row in provenance_rows}
    output = []
    for row in records:
        item = {
            "name": row["name"], "platform": row["platform"], "profile_url": row["profile_url"],
            "follower_count": row["follower_count"], "engagement_rate": row["engagement_rate"],
            "niche": row["niche"], "content_themes": "; ".join(json.loads(str(row["content_themes"]))),
            "content_evidence": row["content_evidence"], "business_email": row["business_email"],
            "website": row["website"], "classification_status": row["classification_status"],
            "classification_reasons": " | ".join(json.loads(str(row["classification_reasons"]))),
            "source_url": row["source_url"], "collected_at": row["collected_at"],
        }
        for field_name in ("follower_count", "engagement_rate", "contact_email", "content_evidence"):
            proof = provenance.get((int(row["id"]), field_name))
            item[f"{field_name}_verification"] = proof["verification_status"] if proof else "MISSING"
            item[f"{field_name}_source_url"] = proof["source_url"] if proof else ""
        output.append(item)
    return pd.DataFrame(output).to_csv(index=False).encode("utf-8-sig")


def message_export(database_path: str) -> bytes:
    """Build an export of current email and Instagram DM draft versions."""
    with connect(database_path) as connection:
        rows = connection.execute("""SELECT i.name,i.business_email AS email,
            m.channel,m.subject,m.body,m.approval_status,m.current_version,m.generated_at
            FROM outreach_messages m JOIN influencers i ON i.id=m.influencer_id
            ORDER BY i.name COLLATE NOCASE,m.channel""").fetchall()
    return pd.DataFrame([dict(row) for row in rows]).to_csv(index=False).encode("utf-8-sig")


def main() -> None:
    st.set_page_config(page_title="Micro-Influencer Outreach", page_icon="📬", layout="wide")
    st.title("AI-Powered Micro-Influencer Outreach Automation")
    st.caption("Discover by CSV, qualify creators, create reviewed drafts, and track simulated outreach.")

    try:
        settings = Settings.from_env(env_file=".env")
        initialize_database(settings.database_path)
        total = count_influencers(settings.database_path)
    except (ValueError, DatabaseError) as exc:
        st.error(f"Application setup error: {exc}")
        st.stop()

    overview_tab, import_tab, profiles_tab, messages_tab, outreach_tab = st.tabs([
        "Overview", "Influencer import", "Profiles & qualification", "Personalized drafts", "Outreach tracker",
    ])
    with overview_tab:
        profiles = list_influencers(settings.database_path)
        decisions = {key: sum(row["classification_status"] == key for row in profiles)
                     for key in ("PASS", "FAIL", "REVIEW_REQUIRED")}
        cols = st.columns(4)
        cols[0].metric("Influencer records", total)
        cols[1].metric("PASS", decisions["PASS"])
        cols[2].metric("FAIL", decisions["FAIL"])
        cols[3].metric("Review required", decisions["REVIEW_REQUIRED"])
        st.info("Email delivery is simulated and logged. No email or Instagram DM is sent from this prototype.")
        if total:
            d1, d2 = st.columns(2)
            d1.download_button("Download influencer dataset CSV", dataset_export(settings.database_path),
                               "influencer_dataset.csv", "text/csv")
            d2.download_button("Download personalized messages CSV", message_export(settings.database_path),
                               "personalized_messages.csv", "text/csv")

    with import_tab:
        st.subheader("Import authorized creator research")
        st.write(
            "Prepare a CSV using the header-only template. Use information from legitimate sources "
            "you are authorized to consult. Include source URLs and collection timestamps. "
            "Values are UNVERIFIED by default; mark VERIFIED only after your own source review."
        )
        st.download_button(
            "Download CSV template",
            data=generate_csv_template(),
            file_name="influencer_import_template.csv",
            mime="text/csv",
        )
        with st.expander("Discover technology creators from YouTube (official API)", expanded=True):
            st.write("Uses the YouTube Data API to search technology, programming, and AI channels, then reads public subscriber counts and recent public video interactions. Emails are never guessed; the API result is marked Not Found.")
            youtube_key = st.text_input("YouTube Data API key", type="password", key="youtube_api_key")
            target_count = st.number_input("Target discovered profiles", min_value=1, max_value=100,
                                           value=50, step=1, key="youtube_target_count")
            if st.button("Discover and import YouTube profiles", type="primary", key="discover_youtube"):
                if not youtube_key.strip():
                    st.error("Enter a YouTube Data API key to run discovery. The key is used for this request and is not saved by the app.")
                else:
                    try:
                        with st.spinner("Searching YouTube and collecting public channel metrics…"):
                            batch = YouTubeAPIProvider(youtube_key).discover(target_count=int(target_count))
                            report = import_batch(settings.database_path, batch)
                        st.success(f"Found {len(batch.records)} eligible profiles. Imported {report.imported_count}; skipped {report.duplicate_count} duplicates; rejected {report.rejected_count}.")
                        if len(batch.records) < int(target_count):
                            st.warning(f"The API returned {len(batch.records)} profiles with public metrics in the requested follower range; try another run/query if you need 50.")
                    except (YouTubeAPIError, DatabaseError, ValueError) as exc:
                        st.error(f"YouTube discovery failed: {exc}")
        with st.expander("Import field guide"):
            st.markdown(
                """
                Required columns: `name`, `platform`, `profile_url`, `source_url`, `collected_at`.
                Optional fields may be left blank. `content_themes` accepts `|` or `;` separators.
                For follower count, engagement rate, contact email, and content evidence, each
                field has a status and optional field-specific source URL. Statuses are `MISSING`,
                `UNVERIFIED`, and `VERIFIED`. An absent email is stored as `Not Found`; no values
                are guessed. Imported profiles are evaluated from available verified evidence; missing or
                unverified requirements stay `REVIEW_REQUIRED`. Duplicate platform/profile URLs are skipped
                unless you explicitly select update mode. For niche evidence,
                use `profile_description` or `content_evidence`; to mark either VERIFIED, provide its own
                `*_source_url`. Themes and declared niche are retained as context, but cannot establish a
                qualification match by themselves. All imports are evaluated and saved to history.
                """
            )

        uploaded = st.file_uploader("Choose a CSV file", type=["csv"], key="influencer_csv")
        if uploaded is not None:
            st.caption(f"Selected: {uploaded.name} ({uploaded.size:,} bytes)")
            update_existing = st.checkbox("Update matching platform/profile records and re-evaluate", value=False)
            if st.button("Validate and import CSV", type="primary"):
                try:
                    provider = CSVDiscoveryProvider()
                    batch = provider.discover(
                        DiscoveryRequest(source_data=uploaded.getvalue(), source_name=uploaded.name)
                    )
                    report = import_batch(settings.database_path, batch, update_existing=update_existing)
                except (ValueError, DatabaseError, csv.Error) as exc:
                    st.error(f"Import could not be completed: {exc}")
                else:
                    if report.imported_count:
                        st.success(f"Imported {report.imported_count}; updated {report.updated_count} record(s).")
                    elif report.updated_count:
                        st.success(f"Updated and re-evaluated {report.updated_count} record(s).")
                    elif report.duplicate_count:
                        st.info("No new records imported; matching profiles were reported as duplicates.")
                    elif report.rejected_count:
                        st.warning("No records were imported; review the rejected-row report below.")
                    else:
                        st.info("No influencer records were present in the CSV.")
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Imported", report.imported_count)
                    c2.metric("Updated", report.updated_count)
                    c3.metric("Duplicates", report.duplicate_count)
                    c4.metric("Rejected", report.rejected_count)
                    if report.duplicates:
                        st.markdown("**Duplicates skipped**")
                        st.dataframe(
                            [{"CSV row": x.row_number, "Profile": x.record_key, "Reason": x.reason} for x in report.duplicates],
                            use_container_width=True,
                        )
                    if report.rejected:
                        st.markdown("**Rejected rows**")
                        st.dataframe(
                            [{"CSV row": x.row_number, "Profile": x.record_key, "Reason": x.reason} for x in report.rejected],
                            use_container_width=True,
                        )
                    if report.imported:
                        st.markdown("**Imported rows**")
                        st.dataframe(
                            [{"CSV row": x.row_number, "Record ID": x.influencer_id, "Profile": x.profile_url} for x in report.imported],
                            use_container_width=True,
                        )
                    if report.updated:
                        st.markdown("**Updated and re-evaluated rows**")
                        st.dataframe(
                            [{"CSV row": x.row_number, "Record ID": x.influencer_id, "Profile": x.profile_url} for x in report.updated],
                            use_container_width=True,
                        )

    with profiles_tab:
        st.subheader("Profiles and qualification")
        with st.expander("Qualification thresholds"):
            rules_valid = True
            try:
                rules = QualificationRules.from_env()
            except ValueError as exc:
                st.error(f"Invalid qualification configuration: {exc}")
                rules = QualificationRules()
                rules_valid = False
            st.write(f"Followers: {rules.min_followers:,}–{rules.max_followers:,}; minimum engagement: {rules.min_engagement_rate:g}%.")
            if st.button("Re-evaluate all profiles", key="reevaluate", disabled=not rules_valid):
                try:
                    count = evaluate_all(settings.database_path, rules)
                    st.success(f"Saved fresh evaluations for {count} profiles.")
                    st.rerun()
                except (DatabaseError, ValueError) as exc:
                    st.error(f"Re-evaluation failed: {exc}")
        profiles = list_influencers(settings.database_path)
        if not profiles:
            st.info("No profiles imported yet.")
        else:
            frame = pd.DataFrame(profiles)
            with connect(settings.database_path) as connection:
                saved_matches = {
                    int(row["influencer_id"]): json.loads(row["evidence_snapshot"]).get("niche_matches", [])
                    for row in connection.execute("""SELECT q.influencer_id,q.evidence_snapshot
                        FROM qualification_evaluations q JOIN (
                            SELECT influencer_id,MAX(id) AS max_id FROM qualification_evaluations GROUP BY influencer_id
                        ) latest ON latest.max_id=q.id""").fetchall()
                }
            frame["niche_labels"] = frame["id"].map(
                lambda influencer_id: ", ".join(sorted({item["niche"] for item in saved_matches.get(int(influencer_id), [])}))
            )
            platform_values = sorted(frame["platform"].dropna().unique().tolist())
            status_values = ["PASS", "FAIL", "REVIEW_REQUIRED"]
            selected_platforms = st.multiselect("Platform", platform_values, default=platform_values)
            selected_statuses = st.multiselect("Qualification", status_values, default=status_values)
            selected_niches = st.multiselect("Niche", ["Technology", "Artificial Intelligence", "Programming"],
                                             default=["Technology", "Artificial Intelligence", "Programming"])
            filtered = frame[frame["platform"].isin(selected_platforms) & frame["classification_status"].isin(selected_statuses)]
            if len(selected_niches) < 3:
                filtered = filtered[filtered["niche_labels"].map(
                    lambda labels: any(niche in labels.split(", ") for niche in selected_niches)
                )]
            st.dataframe(filtered[["id", "name", "platform", "profile_url", "follower_count", "engagement_rate",
                                   "classification_status", "classification_reasons", "business_email"]],
                         use_container_width=True, hide_index=True)
            profile_ids = [int(item["id"]) for item in profiles]
            chosen_id = st.selectbox("Profile details", profile_ids,
                                     format_func=lambda pid: next(f"{p['name']} ({p['platform']})" for p in profiles if p["id"] == pid))
            chosen = next(p for p in profiles if p["id"] == chosen_id)
            left, right = st.columns(2)
            left.markdown("**Qualification reasons**")
            try:
                for reason in json.loads(str(chosen["classification_reasons"])):
                    left.write(f"• {reason}")
            except (ValueError, TypeError):
                left.write("No evaluation saved yet.")
            readiness = email_readiness_for(settings.database_path, chosen_id)
            right.metric("Email available", "Yes" if readiness.email_available else "No")
            right.metric("Email outreach ready", "Yes" if readiness.ready else "No")
            right.caption(readiness.reason)
            st.markdown("**Niche evidence and verification**")
            exact_matches = saved_matches.get(chosen_id, [])
            st.dataframe(pd.DataFrame(exact_matches) if exact_matches else pd.DataFrame(
                columns=["niche", "matched_terms", "excerpt", "source_url", "verification_status"]),
                use_container_width=True, hide_index=True,
                column_config={"source_url": st.column_config.LinkColumn("Evidence source", display_text="Open source")})
            evidence = get_evidence(settings.database_path, chosen_id)
            st.dataframe(pd.DataFrame(evidence) if evidence else pd.DataFrame(columns=["evidence_kind", "evidence_text", "source_url", "verification_status"]),
                         use_container_width=True, hide_index=True,
                         column_config={"source_url": st.column_config.LinkColumn("Evidence source", display_text="Open source")})
            history = evaluation_history(settings.database_path, chosen_id)
            st.markdown("**Evaluation history**")
            for history_index, item in enumerate(history):
                label = "Latest" if history_index == 0 else f"Previous #{len(history) - history_index}"
                with st.expander(f"{label} · {item['outcome']} · {item['evaluated_at']}", expanded=history_index == 0):
                    st.write("**Thresholds used**")
                    st.json(json.loads(str(item["rules_snapshot"])))
                    st.write("**Decision reasons**")
                    for reason in json.loads(str(item["reasons"])):
                        st.write(f"- {reason}")
                    snapshot = json.loads(str(item["evidence_snapshot"]))
                    st.write("**Evidence snapshot**")
                    evidence_frame = pd.DataFrame(snapshot.get("evidence", []))
                    st.dataframe(evidence_frame, use_container_width=True, hide_index=True,
                                 column_config={"source_url": st.column_config.LinkColumn(
                                     "Evidence source", display_text="Open source")})
                    matches_frame = pd.DataFrame(snapshot.get("niche_matches", []))
                    if not matches_frame.empty:
                        st.write("**Matched excerpts**")
                        st.dataframe(matches_frame, use_container_width=True, hide_index=True,
                                     column_config={"source_url": st.column_config.LinkColumn(
                                         "Evidence source", display_text="Open source")})

    with messages_tab:
        st.subheader("AI-personalized outreach drafts")
        st.caption("Only current PASS profiles can generate drafts. Review and approve each channel version manually. No messages are sent here.")
        profiles = list_influencers(settings.database_path)
        qualified = [row for row in profiles if row["classification_status"] == "PASS"]
        if not qualified:
            st.info("There are no currently qualified PASS profiles to generate drafts for.")
        else:
            selected_id = st.selectbox(
                "Qualified creator", [int(row["id"]) for row in qualified],
                format_func=lambda pid: next(f"{row['name']} · {row['platform']}" for row in qualified if row["id"] == pid),
                key="draft_creator",
            )
            selected_profile = next(row for row in qualified if int(row["id"]) == selected_id)
            readiness = email_readiness_for(settings.database_path, selected_id)
            st.write(f"Email available: **{'Yes' if readiness.email_available else 'No'}** — {readiness.reason}")
            st.caption("Email availability does not affect qualification or draft generation. A missing email remains Not Found.")
            drafts = list_current_messages(settings.database_path, selected_id)
            regenerate = bool(drafts)
            button_label = "Regenerate drafts (creates new versions)" if regenerate else "Generate email and DM drafts"
            if st.button(button_label, type="primary", key=f"generate-{selected_id}"):
                try:
                    provider = create_provider(settings)
                    versions = generate_and_save(settings.database_path, selected_id, provider, regenerate=regenerate)
                    st.success(f"Created pending-review versions: email v{versions['EMAIL']}, Instagram DM v{versions['INSTAGRAM_DM']}.")
                    st.rerun()
                except (GenerationError, ProviderError, DatabaseError, ValueError) as exc:
                    st.error(f"Draft generation failed; any existing versions were retained. {exc}")

            for draft in drafts:
                message_id = int(draft["id"])
                channel = str(draft["channel"])
                version = int(draft["current_version"])
                with st.container(border=True):
                    st.markdown(f"### {'Email' if channel == 'EMAIL' else 'Instagram DM'} · version {version}")
                    st.write(f"Approval: **{draft['approval_status']}** · Provider: **{draft['provider'] or 'manual edit'}** · Model: **{draft['model'] or '—'}** · Generated at: **{draft['generated_at'] or 'not generated for this edit'}**")
                    if channel == "EMAIL":
                        st.caption(f"Current email body: {count_words(str(draft['body']))} words (required 60–90). Subject is not counted.")
                    else:
                        st.caption(f"Current DM: {count_words(str(draft['body']))} words (required 15–30).")
                    with st.form(key=f"edit-form-{message_id}-{version}"):
                        subject_value = st.text_input("Subject", value=str(draft["subject"] or ""), key=f"subject-{message_id}-{version}", disabled=True) if channel == "EMAIL" else None
                        st.caption("Keep the approved template intact; only replace the quoted phrase with an exact 3-8-word excerpt from current verified evidence. Free-form factual additions are rejected.")
                        body_value = st.text_area("Editable draft", value=str(draft["body"]), height=220, key=f"body-{message_id}-{version}")
                        save_edit = st.form_submit_button("Save edits as a new pending-review version")
                    if save_edit:
                        try:
                            new_version = edit_current_message(
                                settings.database_path, message_id, subject=subject_value, body=body_value,
                            )
                            st.success(f"Saved version {new_version}. Approval reset to pending review.")
                            st.rerun()
                        except (GenerationError, DatabaseError, ValueError) as exc:
                            st.error(f"Could not save edits: {exc}")
                    current_versions = message_history(settings.database_path, message_id)
                    current_version_record = next((item for item in current_versions
                                                    if int(item["version_number"]) == version), None)
                    if current_version_record:
                        try:
                            current_citations = json.loads(str(current_version_record["evidence_snapshot"])).get("citations", [])
                        except (ValueError, AttributeError):
                            current_citations = []
                        if current_citations:
                            st.write("**Verified evidence used for this version**")
                            st.dataframe(pd.DataFrame(current_citations), use_container_width=True, hide_index=True,
                                         column_config={"source_url": st.column_config.LinkColumn(
                                             "Evidence source", display_text="Open source")})
                    if draft["approval_status"] != "APPROVED":
                        st.caption("Approval applies to the saved version shown here. Save edits first if the text area has changes.")
                        if st.button(f"Approve saved version {version}", key=f"approve-{message_id}-{version}"):
                            try:
                                approve_current_message(settings.database_path, message_id, expected_version=version)
                                st.success("This exact message version is approved for possible later outreach use.")
                                st.rerun()
                            except (GenerationError, DatabaseError, ValueError) as exc:
                                st.error(f"Approval failed: {exc}")
                    versions_history = current_versions
                    with st.expander(f"Version history ({len(versions_history)} versions)"):
                        for historical in versions_history:
                            label = "Current" if int(historical["version_number"]) == version else "Previous"
                            st.markdown(f"**{label} v{historical['version_number']} · {historical['source_type']} · {historical['approval_status']} · {historical['created_at']}**")
                            if historical["provider"]:
                                st.caption(f"Provider/model: {historical['provider']} / {historical['model'] or 'unknown'} · generated {historical['generated_at'] or 'timestamp unavailable'}")
                            st.write(str(historical["body"]))
                            try:
                                citation_rows = json.loads(str(historical["evidence_snapshot"])).get("citations", [])
                            except (ValueError, AttributeError):
                                citation_rows = []
                            if citation_rows:
                                st.dataframe(pd.DataFrame(citation_rows), use_container_width=True, hide_index=True,
                                             column_config={"source_url": st.column_config.LinkColumn(
                                                 "Evidence source", display_text="Open source")})


    with outreach_tab:
        st.subheader("Email outreach simulation")
        st.caption("Only current PASS creators with a verified public email and approved current email draft are eligible. Simulation records a status; it does not deliver an email.")
        candidates = list_send_candidates(settings.database_path)
        if candidates:
            candidate_frame = pd.DataFrame([{
                "Name": row["name"], "Platform": row["platform"], "Email": row["business_email"],
                "Subject": row["subject"], "Draft version": row["current_version"],
            } for row in candidates])
            st.dataframe(candidate_frame, use_container_width=True, hide_index=True)
            labels = {int(row["influencer_id"]): f"{row['name']} · {row['business_email']}" for row in candidates}
            chosen_ids = st.multiselect("Select approved email drafts to simulate", list(labels),
                                        format_func=lambda item: labels[item], key="outreach_selection")
            confirm = st.checkbox("I understand this records a simulated send and does not email anyone.",
                                  key="outreach_simulation_confirm")
            if st.button("Record simulated send(s)", type="primary",
                          disabled=not chosen_ids or not confirm, key="simulate_outreach"):
                completed = 0
                errors = []
                for influencer_id in chosen_ids:
                    try:
                        simulate_email_send(settings.database_path, int(influencer_id))
                        completed += 1
                    except (OutreachNotReady, DatabaseError, ValueError) as exc:
                        errors.append(f"{labels[int(influencer_id)]}: {exc}")
                if completed:
                    st.success(f"Recorded {completed} simulated email send(s). No messages were delivered.")
                for error in errors:
                    st.error(error)
                st.rerun()
        else:
            st.info("No eligible creators yet. Import records, qualify them, generate drafts, verify contact email provenance, and approve the current email draft.")
        st.markdown("**Outreach log**")
        activity = list_outreach_activity(settings.database_path)
        if activity:
            st.dataframe(pd.DataFrame(activity), use_container_width=True, hide_index=True)
        else:
            st.info("No outreach activity has been recorded.")


if __name__ == "__main__":
    main()
