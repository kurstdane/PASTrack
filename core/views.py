# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false

import contextlib
from datetime import timedelta
import json
import mimetypes
import os

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.forms import SetPasswordForm
from django.conf import settings
from django import forms
from django.core.paginator import Paginator
from django.db import models, transaction, connection
from django.db.models import Q, Count
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django.shortcuts import get_object_or_404, redirect, render
from django.http import FileResponse, Http404, HttpResponse
from django.utils.html import format_html

from .forms import (
    CaseDetailsForm,
    CaseRemarkForm,
    ChecklistItemForm,
    ProfileUpdateForm,
    PublicCaseSearchForm,
    ReportFilterForm,
    StaffAccountCreateForm,
    StaffAccountUpdateForm,
    StaffSearchForm,
    SupportFeedbackForm,
)
from .models import AuditLog, Case, CaseDocument, CaseNumber, CaseRemark, CustomUser, FAQItem, SupportFeedback
from .notifications import send_case_email, sns_hook


def _municipality_area_code(name: str) -> str:
    raw = (name or "").strip()
    if not raw:
        return ""
    letters = "".join([c for c in raw.upper() if c.isalpha()])
    if len(letters) >= 3:
        return letters[:3]
    return letters


def _build_checklist_rows(formset, documents: list[CaseDocument]):
    docs_by_key = {((d.doc_type or "").strip().lower()): d for d in (documents or [])}
    rows: list[dict[str, object]] = []
    for f in formset:
        selected = ((f["doc_type"].value() or "").strip())
        custom = ((f["custom_doc_type"].value() or "").strip())
        effective_doc_type = custom if selected == "__custom__" else selected
        key = (effective_doc_type or "").strip().lower()
        doc = docs_by_key.get(key)
        filename = ""
        if doc and getattr(doc, "file", None):
            filename = os.path.basename(getattr(doc.file, "name", "") or "")
        rows.append({
            "form": f,
            "doc": doc,
            "doc_type": effective_doc_type,
            "filename": filename,
        })
    return rows


def _case_type_requirements(case_type: str, *, title_type: str = "") -> list[str]:
    """Minimal requirements list per case type (dropdown + initial checklist)."""
    case_type = (case_type or "").strip()
    title_type = (title_type or "").strip()

    mapping: dict[str, list[str]] = {
        "land_first_time": [
            "Letter-request (Municipal/Provincial Assessor)",
            "Technical Description / Sketch Plan (GE) and DENR-approved Survey Plan",
            "CENRO Certification (alienable and disposable area)",
            "Affidavit of Ownership (long, continuous possession)",
            "Barangay Captain Certification (possession/occupancy, no controversy)",
            "Affidavit of adjoining owners",
            "Ocular inspection/investigation report (Assessor/Staff)",
        ],
        "building_improvements": [
            "Letter-request (Municipal/Provincial Assessor)",
            "Approved building permit + building plan / Certificate of Completion / Occupancy permit",
            "Affidavit of Ownership / Sworn Statement of Market Value (if no building permit)",
            "Affidavit of Consent from land owner (if land owned by another)",
            "Inspection report / FAAS of building/structure (Assessor/Staff)",
            "Registration from Municipal Engineer (machineries)",
        ],
        "subdivision_consolidation": [
            "Letter request (subdivision/consolidation)",
            "Inspection report + endorsement (Assessor/Staff)",
            "Approved subdivision / survey plan",
            "Tax Clearance (current)",
        ],
        "reassessment_reclassification": [
            "Letter request (re-assessment/re-classification)",
            "Inspection report + endorsement (Assessor/Staff)",
            "DAR Clearance / MARO Certification (as applicable)",
            "Tax Clearance (current)",
            "Tax Declaration (photocopy)",
        ],
        "area_increase_decrease": [
            "Letter request (correction of area)",
            "Inspection report + endorsement (Assessor/Staff)",
            "Approved Survey Plan / Technical Description",
            "Affidavit of adjoining owners (if increase)",
            "Tax Clearance (current)",
            "DENR Certification (alienable and disposable area)",
        ],
        "transfer_ownership_tax_decl": [
            "Letter request (transfer of ownership of tax declaration)",
            "Endorsement from Municipal Assessor",
            "Deed of Conveyance (Registry of Deeds)",
            "Tax Clearance (current)",
            "Certificate Authorizing Registration (CAR)",
            "Subdivision / Consolidation Plan",
            "Transfer Tax / Transfer Fee Receipt",
            "Certified true copy / machine copy of title (if titled)",
        ],
    }

    reqs = list(mapping.get(case_type, []))
    if case_type == "transfer_ownership_tax_decl":
        if title_type == "untitled":
            # Remove titled-only document.
            reqs = [r for r in reqs if "machine copy of title" not in (r or "").lower()]
    if case_type == "land_first_time":
        if title_type == "titled":
            # Add title proof when applicable.
            extra = "Certified true copy / machine copy of title"
            if extra.lower() not in {(r or "").lower() for r in reqs}:
                reqs.append(extra)
    return reqs


def landing(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "core/landing.html")


def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("select 1")
            row = cursor.fetchone()
        return HttpResponse("ok" if (row and row[0] == 1) else "db_error", content_type="text/plain")
    except Exception as e:
        return HttpResponse(f"error: {type(e).__name__}: {e}", status=500, content_type="text/plain")


def _public_status_label(case: Case) -> str:
    # Module 4: Simplified, public-friendly status labels.
    status = getattr(case, "status", "")
    mapping = {
        "not_received": "Pending",
        "received": "Received",
        "in_review": "Reviewed",
        "for_taxmapping": "Tax Mapping",
        "for_approval": "Approved",
        "approved": "Approved",
        "for_numbering": "Numbered",
        "for_release": "For Release",
        "released": "Released",
        "client_correction": "Returned to Client (Correction Window)",
        "returned": "Returned",
        "withdrawn": "Withdrawn",
    }
    return mapping.get(status, "In Progress")


def _build_public_timeline(case: Case) -> list[dict[str, object]]:
    """Public timeline (no internal remarks / no actor identities)."""
    events: list[dict[str, object]] = []

    def add(label: str, when):
        if when:
            events.append({"label": label, "when": when})

    # Initial creation
    add("Transaction Created", case.created_at)
    
    # Key transitions from audit logs
    history_qs = (
        AuditLog.objects.filter(target_object=f"Case: {case.tracking_id}")
        .order_by("created_at")
        .only("action", "created_at", "details")
    )

    physically_received_added = False

    for h in history_qs:
        action = getattr(h, "action", "")
        if action == "case_receipt":
            if not physically_received_added:
                add("Physically Received", h.created_at)
                physically_received_added = True
            continue
        if action in {"case_status_change", "case_approval", "case_rejection", "case_release"}:
            details = getattr(h, "details", {}) or {}
            new_status = None
            if isinstance(details, dict):
                new_status = details.get("new_status")
            
            if new_status:
                # If the status change is 'received', handle it carefully to avoid duplicates
                if new_status == "received":
                    if not physically_received_added:
                        add("Physically Received", h.created_at)
                        physically_received_added = True
                    continue

                label = _public_status_label(type("obj", (), {"status": new_status})())
                add(f"Status: {label}", h.created_at)
            continue

    # Add current status if not already reflected (though audit logs should cover it)
    # But for a cleaner timeline, we trust the audit logs for transitions.

    # De-dup by (label, when) - also handle cases where multiple logs happen at same second
    seen = set()
    uniq = []
    for e in events:
        # Use a window for "same time" de-duplication if needed, 
        # but here we'll just de-dup exact label+time
        key = (e["label"], getattr(e["when"], "isoformat", lambda: str(e["when"]))())
        if key in seen:
            continue
        seen.add(key)
        uniq.append(e)
    return uniq


def track_case(request):
    """Module 4.1: Public entry to search by tracking number."""
    form = PublicCaseSearchForm(request.GET or None)
    tracking = ""
    if form.is_valid():
        tracking = form.cleaned_data["q"]
        case = Case.objects.filter(tracking_id__iexact=tracking, lgu_submitted_at__isnull=False).first()
        if case:
            return redirect("track_case_detail", tracking_id=case.tracking_id)
        return render(request, "core/track_not_found.html", {"tracking": tracking, "form": form}, status=404)

    return render(request, "core/track.html", {"form": form, "tracking": tracking})


def track_case_detail(request, tracking_id: str):
    """Module 4.1: Public view of case status summary + timeline."""
    tracking = (tracking_id or "").strip().upper()
    case = Case.objects.filter(tracking_id__iexact=tracking, lgu_submitted_at__isnull=False).first()
    if not case:
        return render(request, "core/track_not_found.html", {"tracking": tracking}, status=404)

    show_internal_status = bool(
        request.user.is_authenticated
        and (_is_capitol_staff(request.user) or getattr(request.user, "role", "") == "super_admin")
    )
    internal_status = dict(Case.STATUS_CHOICES).get(getattr(case, "status", ""), getattr(case, "status", ""))

    public_status = _public_status_label(case)
    timeline = _build_public_timeline(case)

    # Public info: do NOT expose submitter identity, remarks, or documents.
    return render(request, "core/track_case_detail.html", {
        "tracking": case.tracking_id,
        "public_status": public_status,
        "internal_status": internal_status,
        "show_internal_status": show_internal_status,
        "updated_at": case.updated_at,
        "timeline": timeline,
    })


def support(request):
    """Module 4.2: Public support landing page."""
    return render(request, "core/support.html")


def faq(request):
    items = FAQItem.objects.filter(is_published=True).order_by("sort_order", "id")
    return render(request, "core/faq.html", {"items": list(items)})


def submit_feedback(request):
    if request.method == "POST":
        form = SupportFeedbackForm(request.POST)
        if form.is_valid():
            fb = SupportFeedback.objects.create(
                name=(form.cleaned_data.get("name") or "").strip(),
                email=(form.cleaned_data.get("email") or "").strip(),
                message=form.cleaned_data["message"],
            )
            AuditLog.objects.create(
                actor=None,
                action="support_feedback",
                target_object=f"SupportFeedback: {fb.id}",
                details={"public": True},
            )
            messages.success(request, "Thanks! Your message has been sent.")
            return redirect("support")
    else:
        form = SupportFeedbackForm()
    return render(request, "core/feedback.html", {"form": form})


@login_required
def analytics_dashboard(request):
    denial = _require_super_admin(request)
    if denial:
        return denial

    # Module 5.1: High-level metrics
    total_cases = Case.objects.count()
    total_users = CustomUser.objects.count()

    by_status_raw = list(
        Case.objects.values("status").annotate(count=Count("id")).order_by("status")
    )
    status_labels = dict(Case.STATUS_CHOICES)
    by_status = [
        {"status": status_labels.get(r["status"], r["status"]), "count": r["count"]}
        for r in by_status_raw
    ]

    released = Case.objects.filter(status="released", released_at__isnull=False)
    avg_days = None
    if released.exists():
        # Average processing time (created -> released) in days.
        from django.db.models import Avg, ExpressionWrapper, DurationField

        avg_delta = released.annotate(
            delta=ExpressionWrapper(
                (models.F("released_at") - models.F("created_at")),
                output_field=DurationField(),
            )
        ).aggregate(avg=Avg("delta"))
        if avg_delta.get("avg"):
            avg_days = avg_delta["avg"].total_seconds() / 86400

    return render(request, "core/analytics.html", {
        "role_display": request.user.get_role_display(),
        "total_cases": total_cases,
        "total_users": total_users,
        "by_status": by_status,
        "avg_days": avg_days,
    })


@login_required
def reports(request):
    denial = _require_super_admin(request)
    if denial:
        return denial

    form = ReportFilterForm(request.GET or None)
    rows = []
    title = "Reports"

    if form.is_valid():
        report_type = form.cleaned_data["report_type"]
        date_from = form.cleaned_data.get("date_from")
        date_to = form.cleaned_data.get("date_to")
        status = (form.cleaned_data.get("status") or "").strip()
        sort = (form.cleaned_data.get("sort") or "-created_at").strip()

        qs = Case.objects.all()
        if status:
            qs = qs.filter(status=status)
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)
        qs = qs.order_by(sort)

        if report_type == "status_breakdown":
            title = "Status Breakdown"
            status_labels = dict(Case.STATUS_CHOICES)
            raw = list(qs.values("status").annotate(count=Count("id")).order_by("status"))
            rows = [{"status": status_labels.get(r["status"], r["status"]), "count": r["count"]} for r in raw]
        elif report_type == "monthly_accomplishment":
            title = "Monthly Accomplishment"
            # Group by month of created_at
            from django.db.models.functions import TruncMonth

            rows = list(
                qs.annotate(month=TruncMonth("created_at"))
                .values("month")
                .annotate(total=Count("id"))
                .order_by("month")
            )
        else:
            title = "Processing Times"
            # Show released cases with processing time.
            released = qs.filter(status="released", released_at__isnull=False)
            rows = list(
                released.values("tracking_id", "created_at", "released_at")
            )

    return render(request, "core/reports.html", {
        "role_display": request.user.get_role_display(),
        "form": form,
        "title": title,
        "rows": rows,
    })


@login_required
def staff_reports(request):
    if not (_is_capitol_staff(request.user) or request.user.role == "super_admin"):
        messages.error(request, "Not authorized.")
        return redirect("dashboard")

    user = request.user
    activity_raw = list(
        AuditLog.objects.filter(actor=user, action__startswith="case_")
        .values("action")
        .annotate(count=Count("id"))
        .order_by("action")
    )
    activity_labels = dict(AuditLog.ACTION_CHOICES)
    activity_counts = [
        {"action": activity_labels.get(r["action"], r["action"]), "count": r["count"]}
        for r in activity_raw
        if r.get("count")
    ]
    total = sum(int(r.get("count") or 0) for r in activity_raw)

    return render(request, "core/staff_reports.html", {
        "role_display": user.get_role_display(),
        "activity_counts": activity_counts,
        "activity_total": total,
    })


@login_required
def export_reports_csv(request):
    denial = _require_super_admin(request)
    if denial:
        return denial

    form = ReportFilterForm(request.GET or None)
    if not form.is_valid():
        messages.error(request, "Invalid report parameters.")
        return redirect("reports")

    report_type = form.cleaned_data["report_type"]
    date_from = form.cleaned_data.get("date_from")
    date_to = form.cleaned_data.get("date_to")
    status = (form.cleaned_data.get("status") or "").strip()

    qs = Case.objects.all()
    if status:
        qs = qs.filter(status=status)
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)

    import csv

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="report.csv"'
    writer = csv.writer(response)

    if report_type == "status_breakdown":
        writer.writerow(["status", "count"])
        for r in qs.values("status").annotate(count=Count("id")).order_by("status"):
            writer.writerow([dict(Case.STATUS_CHOICES).get(r["status"], r["status"]), r["count"]])
        return response

    if report_type == "monthly_accomplishment":
        from django.db.models.functions import TruncMonth

        writer.writerow(["month", "total"])
        for r in qs.annotate(month=TruncMonth("created_at")).values("month").annotate(total=Count("id")).order_by("month"):
            writer.writerow([r["month"].date().isoformat() if r["month"] else "", r["total"]])
        return response

    # processing_times
    writer.writerow(["tracking_id", "created_at", "released_at", "days"])
    for c in qs.filter(status="released", released_at__isnull=False).order_by("created_at"):
        delta = (c.released_at - c.created_at) if c.released_at and c.created_at else None
        days = round(delta.total_seconds() / 86400, 2) if delta else ""
        writer.writerow([c.tracking_id, c.created_at.isoformat(), c.released_at.isoformat(), days])
    return response


def _require_super_admin(request):
    if not request.user.is_authenticated:
        return redirect("login")
    if getattr(request.user, "role", None) != "super_admin":
        messages.error(request, "Not authorized.")
        return redirect("dashboard")
    return None


def _is_capitol_staff(user) -> bool:
    return bool(getattr(user, "role", "").startswith("capitol_"))


def _user_can_view_case(user: CustomUser, case: Case) -> bool:
    role = getattr(user, "role", "") or ""
    if role == "super_admin" or _is_capitol_staff(user):
        return True
    if role == "lgu_admin":
        user_mun = (getattr(user, "lgu_municipality", "") or "").strip()
        case_mun = (getattr(getattr(case, "submitted_by", None), "lgu_municipality", "") or "").strip()
        if not user_mun or not case_mun:
            return False
        if getattr(case, "status", "") == "client_correction":
            deadline = getattr(case, "client_correction_deadline", None)
            if deadline and timezone.now() > deadline:
                return False
        return user_mun == case_mun
    return False


def _user_is_current_owner_for_internal_sections(user: CustomUser, case: Case) -> bool:
    role = getattr(user, "role", "") or ""
    if role == "super_admin":
        return True
    if role == "capitol_receiving":
        return getattr(case, "status", "") in {"not_received", "received"} and getattr(case, "assigned_to_id", None) is None
    if role == "capitol_examiner":
        return getattr(case, "status", "") in {"for_review", "under_review", "in_review"} and getattr(case, "assigned_to_id", None) == getattr(user, "id", None)
    if role == "capitol_approver":
        return getattr(case, "status", "") == "for_approval"
    if role == "capitol_taxmapper":
        return getattr(case, "status", "") == "for_taxmapping" and getattr(case, "taxmapper_assigned_to_id", None) == getattr(user, "id", None)
    if role == "capitol_numberer":
        return getattr(case, "status", "") == "for_numbering"
    if role == "capitol_releaser":
        return getattr(case, "status", "") == "for_release"
    return False


@login_required
def download_case_document(request, doc_id: int):
    doc = get_object_or_404(CaseDocument.objects.select_related("case"), id=doc_id)
    if not _user_can_view_case(request.user, doc.case):
        raise Http404()
    if not doc.file:
        raise Http404()

    filename = os.path.basename(doc.file.name or "document")
    try:
        fh = doc.file.open("rb")
    except (FileNotFoundError, OSError, ValueError):
        raise Http404()

    response = FileResponse(fh, as_attachment=False, filename=filename)
    guessed, _ = mimetypes.guess_type(filename)
    if guessed:
        response["Content-Type"] = guessed
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@require_POST
def review_case_document(request, doc_id: int):
    doc = get_object_or_404(CaseDocument.objects.select_related("case"), id=doc_id)
    case = doc.case
    if not _user_can_view_case(request.user, case):
        raise Http404()
    if not _user_is_current_owner_for_internal_sections(request.user, case):
        messages.error(request, "Not authorized to review documents for this case right now.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    ok_raw = (request.POST.get("reviewed_ok") or "").strip().lower()
    reviewed_ok = ok_raw in {"1", "true", "yes", "y", "on"}
    review_remark = (request.POST.get("review_remark") or "").strip()

    now = timezone.now()
    has_review_payload = bool(reviewed_ok or review_remark)

    if not has_review_payload:
        reviewed_ok = False
        review_remark = ""
        doc.reviewed_by = None
        doc.reviewed_at = None
    else:
        doc.reviewed_by = request.user
        doc.reviewed_at = now

    doc.reviewed_ok = reviewed_ok
    doc.review_remark = review_remark if (review_remark or not reviewed_ok) else ""
    doc.save(update_fields=["reviewed_ok", "review_remark", "reviewed_by", "reviewed_at", "updated_at"])

    AuditLog.objects.create(
        actor=request.user,
        action="case_document_review",
        target_object=f"Case: {case.tracking_id}",
        details={"document": doc.doc_type, "checked": reviewed_ok, "remark": review_remark[:2000]},
    )

    messages.success(request, "Document review saved.")
    return redirect("case_detail", tracking_id=case.tracking_id)


@login_required
@require_POST
def review_case_documents(request, tracking_id: str):
    case = get_object_or_404(Case, tracking_id=tracking_id)
    if not _user_can_view_case(request.user, case):
        raise Http404()
    if not _user_is_current_owner_for_internal_sections(request.user, case):
        messages.error(request, "Not authorized to review documents for this case right now.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    raw_ids = request.POST.getlist("doc_id") or []
    doc_ids: list[int] = []
    for v in raw_ids:
        if str(v).isdigit():
            doc_ids.append(int(v))
    if not doc_ids:
        messages.error(request, "No documents to save.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    docs = list(CaseDocument.objects.filter(case=case, id__in=doc_ids).order_by("id"))
    by_id = {d.id: d for d in docs}
    missing = [i for i in doc_ids if i not in by_id]
    if missing:
        raise Http404()

    updates: list[dict[str, object]] = []
    now = timezone.now()

    with transaction.atomic():
        for doc in docs:
            reviewed_ok = (request.POST.get(f"reviewed_ok_{doc.id}") or "").strip() == "1"
            review_remark = (request.POST.get(f"review_remark_{doc.id}") or "").strip()
            has_review_payload = bool(reviewed_ok or review_remark)

            new_reviewed_ok = reviewed_ok
            new_review_remark = review_remark if (review_remark or not reviewed_ok) else ""

            if not has_review_payload:
                new_reviewed_ok = False
                new_review_remark = ""

            if (
                doc.reviewed_ok == new_reviewed_ok
                and (doc.review_remark or "") == new_review_remark
                and (doc.reviewed_by_id is None) == (not has_review_payload)
                and (doc.reviewed_at is None) == (not has_review_payload)
            ):
                continue

            doc.reviewed_ok = new_reviewed_ok
            doc.review_remark = new_review_remark
            doc.reviewed_by = None if not has_review_payload else request.user
            doc.reviewed_at = None if not has_review_payload else now
            doc.save(update_fields=["reviewed_ok", "review_remark", "reviewed_by", "reviewed_at", "updated_at"])

            updates.append({"document": doc.doc_type, "checked": new_reviewed_ok, "remark": new_review_remark[:500]})

    if updates:
        AuditLog.objects.create(
            actor=request.user,
            action="case_document_review",
            target_object=f"Case: {case.tracking_id}",
            details={"documents": updates[:50]},
        )

    messages.success(request, "Documents saved.")
    return redirect("case_detail", tracking_id=case.tracking_id)


def _format_audit_details(details) -> str:
    if details is None or details == "":
        return "—"

    if isinstance(details, str):
        s = details.strip()
        if not s:
            return "—"
        try:
            details = json.loads(s)
        except Exception:
            return details

    if isinstance(details, dict):
        parts: list[str] = []

        reason = details.get("reason")
        if reason:
            parts.append(f"Reason: {reason}")

        new_status = details.get("new_status")
        if new_status:
            status_label = dict(Case.STATUS_CHOICES).get(str(new_status), str(new_status))
            parts.append(f"New status: {status_label}")

        for k in sorted(details.keys()):
            if k in {"reason", "new_status"}:
                continue
            v = details.get(k)
            if v is None or v == "":
                continue
            label = str(k).replace("_", " ").strip().title()
            parts.append(f"{label}: {v}")

        return "\n".join(parts) if parts else "—"

    if isinstance(details, list):
        lines = [str(x) for x in details if x is not None and str(x).strip() != ""]
        return "\n".join(lines) if lines else "—"

    return str(details)


def _format_case_history_details(action: str, details) -> str:
    if details is None or details == "":
        details_obj: object = {}
    elif isinstance(details, dict):
        details_obj = details
    elif isinstance(details, str):
        s = details.strip()
        if not s:
            details_obj = {}
        else:
            try:
                details_obj = json.loads(s)
            except Exception:
                details_obj = {"details": s}
    else:
        details_obj = {"details": str(details)}

    d = details_obj if isinstance(details_obj, dict) else {}
    parts: list[str] = []

    if action == "case_assignment":
        assigned_to = (d.get("assigned_to") or "").strip() if isinstance(d.get("assigned_to"), str) else d.get("assigned_to")
        if assigned_to:
            parts.append(f"Assigned to: {assigned_to}")
    elif action in {"case_receipt"}:
        new_status = d.get("new_status")
        if new_status:
            status_label = dict(Case.STATUS_CHOICES).get(str(new_status), str(new_status))
            parts.append(f"Status: {status_label}")
    elif action in {"case_status_change", "case_approval", "case_rejection", "case_release"}:
        new_status = d.get("new_status")
        if new_status:
            status_label = dict(Case.STATUS_CHOICES).get(str(new_status), str(new_status))
            parts.append(f"Status: {status_label}")

        nums = d.get("numbers")
        if isinstance(nums, list) and nums:
            parts.append(f"Numbers: {', '.join(str(n) for n in nums)}")

        reason = d.get("reason")
        if isinstance(reason, str) and reason.strip():
            parts.append(f"Reason: {reason.strip()}")

        returned_to = d.get("returned_to")
        if isinstance(returned_to, str) and returned_to.strip():
            parts.append(f"Returned to: {returned_to.strip()}")
    else:
        extra = _format_audit_details(details)
        if extra and extra != "—":
            parts.append(extra)

    return "\n".join(parts) if parts else "—"

@login_required
def dashboard(request):
    user = request.user
    context = {
        "user": user,
        "role_display": user.get_role_display(),
    }

    if user.role == "super_admin":
        total_users = CustomUser.objects.exclude(id=user.id).count()
        total_cases = Case.objects.exclude(status="draft").count()
        pending_intake = Case.objects.filter(status="not_received").count()
        for_approval = Case.objects.filter(status="for_approval").count()
        released = Case.objects.filter(status="released").count()
        context.update({
            "section": "super_admin",
            "total_users": total_users,
            "stat_cards": [
                {"value": total_users, "label": "Total Staff Accounts"},
                {"value": total_cases, "label": "Total Transactions"},
                {"value": pending_intake, "label": "Pending Intake"},
                {"value": for_approval, "label": "For Approval"},
                {"value": released, "label": "Released"},
            ],
        })
        template = "core/dashboard_superadmin.html"

    elif user.role == "lgu_admin":
        tab = (request.GET.get("tab") or "").strip().lower() or "all"
        q = (request.GET.get("q") or "").strip()

        mun = (getattr(user, "lgu_municipality", "") or "").strip()
        base_qs = Case.objects.filter(lgu_submitted_at__isnull=False).select_related("submitted_by").order_by("-created_at")
        if mun:
            base_qs = base_qs.filter(submitted_by__lgu_municipality=mun)
        else:
            base_qs = base_qs.filter(submitted_by=user)

        base_qs = base_qs.exclude(status="client_correction", client_correction_deadline__lt=timezone.now())

        tab_map = {
            "all": None,
            "pending": {"not_received", "client_correction"},
            "received": {"received", "in_review", "for_taxmapping", "for_approval", "for_numbering", "for_release", "released"},
        }
        statuses = tab_map.get(tab)
        qs = base_qs
        if statuses:
            qs = qs.filter(status__in=statuses)

        if q:
            qs = qs.filter(
                Q(tracking_id__icontains=q) |
                Q(client_name__icontains=q) |
                Q(client_email__icontains=q) |
                Q(submitted_by__lgu_municipality__icontains=q) |
                Q(case_type__icontains=q)
            )

        # Counts for tab badges (computed on the municipality-wide base set)
        all_count = base_qs.count()
        pending_count = base_qs.filter(status__in=tab_map["pending"]).count()
        received_count = base_qs.filter(status__in=tab_map["received"]).count()
        released_count = base_qs.filter(status="released").count()
        correction_count = base_qs.filter(status="client_correction").count()

        paginator = Paginator(qs, 10)
        page_obj = paginator.get_page(request.GET.get("page") or 1)

        raw = list(base_qs.values("status").annotate(count=Count("id")).order_by("status"))
        status_labels = dict(Case.STATUS_CHOICES)
        status_counts = [{"status": status_labels.get(r["status"], r["status"]), "count": r["count"]} for r in raw]

        context.update({
            "section": "lgu_admin",
            "tab": tab,
            "tabs": [("all", "All", all_count), ("pending", "Pending", pending_count), ("received", "Received", received_count)],
            "page_obj": page_obj,
            "status_counts": status_counts,
            "filter_q": q,
            "stat_cards": [
                {"value": all_count, "label": "Total Submissions"},
                {"value": pending_count, "label": "Pending"},
                {"value": received_count, "label": "In Process"},
                {"value": released_count, "label": "Released"},
                {"value": correction_count, "label": "For Correction"},
            ],
        })
        template = "core/dashboard_lgu.html"

    else:  # Capitol roles
        activity_raw = list(
            AuditLog.objects.filter(actor=user, action__startswith="case_")
            .values("action")
            .annotate(count=Count("id"))
            .order_by("action")
        )
        activity_labels = dict(AuditLog.ACTION_CHOICES)
        activity_counts = [
            {"action": activity_labels.get(r["action"], r["action"]), "count": r["count"]}
            for r in activity_raw
            if r.get("count")
        ]
        context.update({
            "activity_counts": activity_counts,
            "activity_total": sum(int(r.get("count") or 0) for r in activity_raw),
        })

        context.update({
            "section": "capitol_staff",
            "capitol_role": user.get_role_display(),
        })

        if user.role == "capitol_receiving":
            tab = (request.GET.get("tab") or "").strip().lower() or "pending"
            q = (request.GET.get("q") or "").strip()
            status_filter = (request.GET.get("status") or "").strip()
            lgu_filter = (request.GET.get("lgu") or "").strip()
            type_filter = (request.GET.get("case_type") or "").strip()

            base_pending_qs = Case.objects.filter(status="not_received")
            base_received_qs = Case.objects.filter(status="received", assigned_to__isnull=True)

            if q:
                base_pending_qs = base_pending_qs.filter(
                    Q(tracking_id__icontains=q) |
                    Q(client_name__icontains=q) |
                    Q(client_email__icontains=q) |
                    Q(submitted_by__lgu_municipality__icontains=q) |
                    Q(case_type__icontains=q)
                )
                base_received_qs = base_received_qs.filter(
                    Q(tracking_id__icontains=q) |
                    Q(client_name__icontains=q) |
                    Q(client_email__icontains=q) |
                    Q(submitted_by__lgu_municipality__icontains=q) |
                    Q(case_type__icontains=q)
                )

            if lgu_filter:
                base_pending_qs = base_pending_qs.filter(submitted_by__lgu_municipality=lgu_filter)
                base_received_qs = base_received_qs.filter(submitted_by__lgu_municipality=lgu_filter)

            if type_filter:
                base_pending_qs = base_pending_qs.filter(case_type=type_filter)
                base_received_qs = base_received_qs.filter(case_type=type_filter)

            if status_filter:
                base_pending_qs = base_pending_qs.filter(status=status_filter)
                base_received_qs = base_received_qs.filter(status=status_filter)

            pending_qs = base_pending_qs.select_related("submitted_by").order_by("-created_at")
            received_qs = base_received_qs.select_related("submitted_by").order_by("-received_at")

            pending_count = pending_qs.count()
            received_count = received_qs.count()

            if tab == "received":
                paginator = Paginator(received_qs, 10)
            else:
                tab = "pending"
                paginator = Paginator(pending_qs, 10)
            page_obj = paginator.get_page(request.GET.get("page") or 1)

            today = timezone.localdate()
            stats_received_today = Case.objects.filter(received_by=user, received_at__date=today).count()
            stats_pending_intake = Case.objects.filter(status="not_received").count()
            stats_for_assignment = Case.objects.filter(status="received", assigned_to__isnull=True).count()
            stats_returned_to_owner = Case.objects.filter(status="client_correction").count()
            stats_total_handled = AuditLog.objects.filter(actor=user, action__in={"case_receipt", "case_assignment", "case_return"}).count()

            ready_for_assignment = Case.objects.filter(status="received", assigned_to__isnull=True).select_related("submitted_by").order_by("-received_at")[:8]
            returned_to_me = Case.objects.filter(returned_by=user).select_related("submitted_by").order_by("-returned_at")[:8]
            recent_activity = AuditLog.objects.filter(actor=user).order_by("-created_at")[:10]

            context.update({
                "tab": tab,
                "tabs": [("pending", "Pending", pending_count), ("received", "Received", received_count)],
                "page_obj": page_obj,
                "filter_q": q,
                "filter_status": status_filter,
                "filter_lgu": lgu_filter,
                "filter_case_type": type_filter,
                "lgu_choices": CustomUser.LGU_MUNICIPALITY_CHOICES,
                "case_type_choices": Case.CASE_TYPE_CHOICES,
                "receiver_stats": {
                    "received_today": stats_received_today,
                    "pending_intake": stats_pending_intake,
                    "for_assignment": stats_for_assignment,
                    "returned_to_owner": stats_returned_to_owner,
                    "total_handled": stats_total_handled,
                },
                "stat_cards": [
                    {"value": stats_received_today, "label": "Received Today"},
                    {"value": stats_pending_intake, "label": "Pending Intake"},
                    {"value": stats_for_assignment, "label": "For Assignment"},
                    {"value": stats_returned_to_owner, "label": "Returned to Owner"},
                    {"value": stats_total_handled, "label": "Total Transactions Handled"},
                ],
                "ready_for_assignment": ready_for_assignment,
                "returned_to_me": returned_to_me,
                "recent_activity": recent_activity,
            })

        elif user.role == "capitol_examiner":
            q = (request.GET.get("q") or "").strip()
            lgu_filter = (request.GET.get("lgu") or "").strip()
            type_filter = (request.GET.get("case_type") or "").strip()
            status_filter = (request.GET.get("status") or "").strip()

            base_all = Case.objects.filter(assigned_to=user).exclude(status="draft")
            qs = base_all.select_related("submitted_by").order_by("-assigned_at", "-updated_at")

            if q:
                qs = qs.filter(
                    Q(tracking_id__icontains=q) |
                    Q(client_name__icontains=q) |
                    Q(client_email__icontains=q) |
                    Q(submitted_by__lgu_municipality__icontains=q) |
                    Q(case_type__icontains=q)
                )
            if lgu_filter:
                qs = qs.filter(submitted_by__lgu_municipality=lgu_filter)
            if type_filter:
                qs = qs.filter(case_type=type_filter)
            if status_filter:
                qs = qs.filter(status=status_filter)

            paginator = Paginator(qs, 10)
            page_obj = paginator.get_page(request.GET.get("page") or 1)

            today = timezone.localdate()
            reviewed_today = AuditLog.objects.filter(actor=user, action="case_document_review", created_at__date=today).count()

            context.update({
                "page_obj": page_obj,
                "filter_q": q,
                "filter_status": status_filter,
                "filter_lgu": lgu_filter,
                "filter_case_type": type_filter,
                "lgu_choices": CustomUser.LGU_MUNICIPALITY_CHOICES,
                "case_type_choices": Case.CASE_TYPE_CHOICES,
                "status_choices": Case.STATUS_CHOICES,
                "stat_cards": [
                    {"value": base_all.count(), "label": "Assigned to Me"},
                    {"value": base_all.filter(status="in_review").count(), "label": "In Review"},
                    {"value": base_all.filter(status="for_taxmapping").count(), "label": "For Taxmapping"},
                    {"value": base_all.filter(status="for_approval").count(), "label": "For Approval"},
                    {"value": reviewed_today, "label": "Docs Reviewed Today"},
                ],
            })

        elif user.role == "capitol_approver":
            q = (request.GET.get("q") or "").strip()
            lgu_filter = (request.GET.get("lgu") or "").strip()
            type_filter = (request.GET.get("case_type") or "").strip()

            base_all = Case.objects.filter(status="for_approval")
            qs = base_all.select_related("submitted_by").order_by("-updated_at")

            if q:
                qs = qs.filter(
                    Q(tracking_id__icontains=q) |
                    Q(client_name__icontains=q) |
                    Q(client_email__icontains=q) |
                    Q(submitted_by__lgu_municipality__icontains=q) |
                    Q(case_type__icontains=q)
                )
            if lgu_filter:
                qs = qs.filter(submitted_by__lgu_municipality=lgu_filter)
            if type_filter:
                qs = qs.filter(case_type=type_filter)

            paginator = Paginator(qs, 10)
            page_obj = paginator.get_page(request.GET.get("page") or 1)

            today = timezone.localdate()
            approved_today = AuditLog.objects.filter(actor=user, action="case_approval", created_at__date=today).count()
            rejected_today = AuditLog.objects.filter(actor=user, action="case_rejection", created_at__date=today).count()

            context.update({
                "page_obj": page_obj,
                "filter_q": q,
                "filter_lgu": lgu_filter,
                "filter_case_type": type_filter,
                "lgu_choices": CustomUser.LGU_MUNICIPALITY_CHOICES,
                "case_type_choices": Case.CASE_TYPE_CHOICES,
                "stat_cards": [
                    {"value": base_all.count(), "label": "For Approval"},
                    {"value": approved_today, "label": "Approved Today"},
                    {"value": rejected_today, "label": "Rejected Today"},
                    {"value": Case.objects.filter(status="for_release").count(), "label": "For Release"},
                    {"value": context.get("activity_total", 0), "label": "My Total Actions"},
                ],
            })

        elif user.role == "capitol_taxmapper":
            q = (request.GET.get("q") or "").strip()
            lgu_filter = (request.GET.get("lgu") or "").strip()
            type_filter = (request.GET.get("case_type") or "").strip()

            base_all = Case.objects.filter(status="for_taxmapping", taxmapper_assigned_to=user)
            qs = base_all.select_related("submitted_by").order_by("-updated_at")

            if q:
                qs = qs.filter(
                    Q(tracking_id__icontains=q) |
                    Q(client_name__icontains=q) |
                    Q(client_email__icontains=q) |
                    Q(submitted_by__lgu_municipality__icontains=q) |
                    Q(case_type__icontains=q)
                )
            if lgu_filter:
                qs = qs.filter(submitted_by__lgu_municipality=lgu_filter)
            if type_filter:
                qs = qs.filter(case_type=type_filter)

            paginator = Paginator(qs, 10)
            page_obj = paginator.get_page(request.GET.get("page") or 1)

            today = timezone.localdate()
            status_changed_today = AuditLog.objects.filter(actor=user, action="case_status_change", created_at__date=today).count()

            context.update({
                "page_obj": page_obj,
                "filter_q": q,
                "filter_lgu": lgu_filter,
                "filter_case_type": type_filter,
                "lgu_choices": CustomUser.LGU_MUNICIPALITY_CHOICES,
                "case_type_choices": Case.CASE_TYPE_CHOICES,
                "stat_cards": [
                    {"value": base_all.count(), "label": "My Taxmapping Queue"},
                    {"value": status_changed_today, "label": "Status Updates Today"},
                    {"value": Case.objects.filter(status="for_approval").count(), "label": "For Approval"},
                    {"value": Case.objects.filter(status="client_correction").count(), "label": "For Correction"},
                    {"value": context.get("activity_total", 0), "label": "My Total Actions"},
                ],
            })

        elif user.role == "capitol_numberer":
            queue_cases = (
                Case.objects.filter(status="for_numbering")
                .select_related("submitted_by")
                .prefetch_related("numbers")
                .order_by("-updated_at")
            )

            lgu = (request.GET.get("lgu") or "").strip()
            date_from_raw = (request.GET.get("date_from") or "").strip()
            date_to_raw = (request.GET.get("date_to") or "").strip()
            number_q = (request.GET.get("number") or "").strip()

            date_from = parse_date(date_from_raw) if date_from_raw else None
            date_to = parse_date(date_to_raw) if date_to_raw else None

            if lgu:
                queue_cases = queue_cases.filter(submitted_by__lgu_municipality=lgu)
            if date_from:
                queue_cases = queue_cases.filter(created_at__date__gte=date_from)
            if date_to:
                queue_cases = queue_cases.filter(created_at__date__lte=date_to)
            if number_q:
                if number_q.isdigit():
                    padded = number_q.zfill(5) if len(number_q) <= 5 else number_q
                    queue_cases = queue_cases.filter(
                        Q(numbers__number=padded) |
                        Q(tracking_id__icontains=number_q)
                    )
                else:
                    queue_cases = queue_cases.filter(
                        Q(tracking_id__icontains=number_q)
                    )
                queue_cases = queue_cases.distinct()

            last_used = CaseNumber.objects.order_by("-number").values_list("number", flat=True).first()
            suggested_next = (int(last_used) + 1) if (last_used and str(last_used).isdigit()) else 1
            suggested_next_str = str(suggested_next).zfill(5)

            context.update({
                "queue_cases": queue_cases[:50],
                "numberer_lgu_choices": CustomUser.LGU_MUNICIPALITY_CHOICES,
                "filter_lgu": lgu,
                "filter_date_from": date_from_raw,
                "filter_date_to": date_to_raw,
                "filter_number": number_q,
                "last_used_number": last_used,
                "suggested_next_number": suggested_next_str,
                "stat_cards": [
                    {"value": Case.objects.filter(status="for_numbering").count(), "label": "For Numbering"},
                    {"value": last_used or "—", "label": "Last Used Number"},
                    {"value": suggested_next_str, "label": "Suggested Next"},
                    {"value": Case.objects.filter(status="for_release").count(), "label": "For Release"},
                    {"value": context.get("activity_total", 0), "label": "My Total Actions"},
                ],
            })

        elif user.role == "capitol_releaser":
            q = (request.GET.get("q") or "").strip()
            lgu_filter = (request.GET.get("lgu") or "").strip()
            type_filter = (request.GET.get("case_type") or "").strip()
            date_numbered_filter = (request.GET.get("date_numbered") or "").strip()

            base_all = Case.objects.filter(status="for_release")
            qs = base_all.select_related("submitted_by").prefetch_related("numbers").order_by("-updated_at")

            if q:
                qs = qs.filter(
                    Q(tracking_id__icontains=q) |
                    Q(client_name__icontains=q) |
                    Q(client_email__icontains=q) |
                    Q(submitted_by__lgu_municipality__icontains=q) |
                    Q(case_type__icontains=q)
                )
            if lgu_filter:
                qs = qs.filter(submitted_by__lgu_municipality=lgu_filter)
            if type_filter:
                qs = qs.filter(case_type=type_filter)

            paginator = Paginator(qs, 10)
            page_obj = paginator.get_page(request.GET.get("page") or 1)

            today = timezone.localdate()
            pending_release = base_all.count()
            released_today_count = Case.objects.filter(status="released", released_at__date=today).count()
            total_released = Case.objects.filter(status="released").count()
            
            cases_released_today = Case.objects.filter(status="released", released_at__date=today).select_related("submitted_by").order_by("-released_at")[:5]
            
            seven_days_ago = timezone.now() - timedelta(days=7)
            unclaimed_cases = Case.objects.filter(status="released", released_at__lt=seven_days_ago).select_related("submitted_by").order_by("-released_at")[:5]
            
            recent_activity = AuditLog.objects.filter(actor=user, action__startswith="case_").order_by("-created_at")[:10]

            context.update({
                "page_obj": page_obj,
                "filter_q": q,
                "filter_lgu": lgu_filter,
                "filter_case_type": type_filter,
                "filter_date_numbered": date_numbered_filter,
                "lgu_choices": CustomUser.LGU_MUNICIPALITY_CHOICES,
                "case_type_choices": Case.CASE_TYPE_CHOICES,
                "stat_cards": [
                    {"value": pending_release, "label": "Pending Release"},
                    {"value": released_today_count, "label": "Released Today"},
                    {"value": total_released, "label": "Total Released"},
                ],
                "cases_released_today": cases_released_today,
                "unclaimed_cases": unclaimed_cases,
                "recent_activity": recent_activity,
            })

        template = "core/dashboard_capitol.html"

    return render(request, template, context)


@login_required
def user_management(request):
    denial = _require_super_admin(request)
    if denial:
        return denial

    form = StaffSearchForm(request.GET or None)
    users_qs = CustomUser.objects.exclude(id=request.user.id).order_by("-date_joined")

    if form.is_valid():
        q = (form.cleaned_data.get("q") or "").strip()
        role = (form.cleaned_data.get("role") or "").strip()

        if q:
            users_qs = users_qs.filter(
                Q(email__icontains=q) |
                Q(full_name__icontains=q) |
                Q(username__icontains=q)
            )
        if role:
            users_qs = users_qs.filter(role=role)

    paginator = Paginator(users_qs, 10)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    return render(request, "core/user_management.html", {
        "role_display": request.user.get_role_display(),
        "search_form": form,
        "page_obj": page_obj,
    })


@login_required
def audit_logs(request):
    denial = _require_super_admin(request)
    if denial:
        return denial

    qs = AuditLog.objects.select_related("actor", "target_user").all()
    action = (request.GET.get("action") or "").strip()
    q = (request.GET.get("q") or "").strip()

    if action:
        qs = qs.filter(action=action)
    if q:
        qs = qs.filter(
            Q(target_object__icontains=q) |
            Q(actor__email__icontains=q) |
            Q(target_user__email__icontains=q)
        )

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    return render(request, "core/audit_logs.html", {
        "role_display": request.user.get_role_display(),
        "page_obj": page_obj,
        "action_filter": action,
        "q_filter": q,
        "actions": AuditLog.ACTION_CHOICES,
    })


@login_required
def export_audit_logs_csv(request):
    denial = _require_super_admin(request)
    if denial:
        return denial

    qs = AuditLog.objects.select_related("actor", "target_user").all()
    action = (request.GET.get("action") or "").strip()
    q = (request.GET.get("q") or "").strip()
    if action:
        qs = qs.filter(action=action)
    if q:
        qs = qs.filter(
            Q(target_object__icontains=q) |
            Q(actor__email__icontains=q) |
            Q(target_user__email__icontains=q)
        )

    import csv
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="audit_logs.csv"'
    writer = csv.writer(response)
    writer.writerow(["created_at", "action", "actor_email", "target_user_email", "target_object", "ip_address"])
    for row in qs.order_by("-created_at"):
        writer.writerow([
            row.created_at.isoformat(),
            row.action,
            getattr(row.actor, "email", "") if row.actor else "",
            getattr(row.target_user, "email", "") if row.target_user else "",
            row.target_object,
            row.ip_address or "",
        ])
    return response


@login_required
def create_staff_account(request):
    denial = _require_super_admin(request)
    if denial:
        return denial

    if request.method == "POST":
        form = StaffAccountCreateForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            temp_password = user.generate_temp_password()
            user.set_password(temp_password)
            # Pending Activation until the user activates and sets a new password.
            user.must_change_password = False
            user.account_status = "pending"
            user.save(created_by=request.user)

            try:
                activation_link = user.issue_activation(
                    request=request,
                    temp_password=temp_password,
                    send_email=getattr(settings, "LEGALTRACK_SEND_EMAILS", True),
                )
            except Exception as e:
                import sys
                print(f"[SMTP-CREATE-ERROR] FAILED: {e}", file=sys.stderr)
                # We still created the user, but email failed. 
                # The user_created.html template can show the link if LEGALTRACK_SHOW_ACTIVATION_LINK is True.
                activation_link = f"Error sending email: {e}"
                messages.warning(request, f"User account created, but activation email failed: {type(e).__name__}: {e}")

            activation_sent = bool(getattr(settings, "LEGALTRACK_SEND_EMAILS", True))
            show_activation_link = bool(getattr(settings, "LEGALTRACK_SHOW_ACTIVATION_LINK", False))

            AuditLog.objects.create(
                actor=request.user,
                action="activation_email_sent",
                target_user=user,
                target_object=f"User: {user.email}",
                details={"account_status": user.account_status}
            )

            return render(request, "core/user_created.html", {
                "role_display": request.user.get_role_display(),
                "created_user": user,
                "temp_password": temp_password,
                "activation_sent": activation_sent,
                "activation_link": activation_link,
                "show_activation_link": show_activation_link,
            })
    else:
        form = StaffAccountCreateForm()

    return render(request, "core/user_create.html", {
        "role_display": request.user.get_role_display(),
        "form": form,
    })


@login_required
def edit_staff_account(request, user_id):
    denial = _require_super_admin(request)
    if denial:
        return denial

    target = get_object_or_404(CustomUser, id=user_id)
    if target.id == request.user.id:
        messages.error(request, "You cannot edit your own account here.")
        return redirect("user_management")

    if request.method == "POST":
        form = StaffAccountUpdateForm(request.POST, instance=target)
        if form.is_valid():
            before = {
                "full_name": target.full_name,
                "designation": target.designation,
                "position": target.position,
                "lgu_municipality": target.lgu_municipality,
            }
            updated = form.save()
            after = {
                "full_name": updated.full_name,
                "designation": updated.designation,
                "position": updated.position,
                "lgu_municipality": updated.lgu_municipality,
            }

            AuditLog.objects.create(
                actor=request.user,
                action="update_user",
                target_user=updated,
                target_object=f"User: {updated.email}",
                details={"before": before, "after": after}
            )

            messages.success(request, "User details updated.")
            return redirect("user_management")
    else:
        form = StaffAccountUpdateForm(instance=target)

    return render(request, "core/user_edit.html", {
        "role_display": request.user.get_role_display(),
        "target_user": target,
        "form": form,
    })


@login_required
@require_POST
def toggle_staff_active(request, user_id):
    denial = _require_super_admin(request)
    if denial:
        return denial

    target = get_object_or_404(CustomUser, id=user_id)
    if target.id == request.user.id:
        messages.error(request, "You cannot change your own status.")
        return redirect("user_management")

    if target.account_status == "active":
        target.account_status = "inactive"
        target.is_active = False
        target.save(update_fields=["account_status", "is_active"])

        AuditLog.objects.create(
            actor=request.user,
            action="deactivate_user",
            target_user=target,
            target_object=f"User: {target.email}",
            details={"account_status": target.account_status}
        )

        messages.success(request, "Account deactivated.")
        return redirect("user_management")

    # Reactivation path
    if target.account_status == "inactive":
        # If never activated, restore to pending and require activation link.
        if target.activated_at is None:
            target.account_status = "pending"
            target.is_active = False
            target.save(update_fields=["account_status", "is_active"])
            messages.info(request, "Account restored to Pending Activation. Use Resend Activation to onboard the user.")
            return redirect("user_management")

        target.account_status = "active"
        target.is_active = True
        target.save(update_fields=["account_status", "is_active"])

        AuditLog.objects.create(
            actor=request.user,
            action="reactivate_user",
            target_user=target,
            target_object=f"User: {target.email}",
            details={"account_status": target.account_status}
        )

        messages.success(request, "Account reactivated.")
        return redirect("user_management")

    # Pending accounts can't be directly activated by Super Admin toggle.
    messages.info(request, "This account is Pending Activation. Use Resend Activation if needed.")
    return redirect("user_management")


@login_required
@require_POST
def resend_activation(request, user_id):
    denial = _require_super_admin(request)
    if denial:
        return denial

    target = get_object_or_404(CustomUser, id=user_id)
    if target.account_status != "pending":
        messages.info(request, "Activation can only be resent for Pending Activation accounts.")
        return redirect("user_management")

    temp_password = target.generate_temp_password()
    target.set_password(temp_password)
    target.temp_password_created_at = timezone.now()
    target.save(update_fields=["password", "temp_password_created_at"])

    try:
        activation_link = target.issue_activation(
            request=request,
            temp_password=temp_password,
            send_email=getattr(settings, "LEGALTRACK_SEND_EMAILS", True),
        )
    except Exception as e:
        import sys
        print(f"[SMTP-RESEND-ERROR] FAILED: {e}", file=sys.stderr)
        messages.error(request, f"Failed to send activation email: {type(e).__name__}: {e}")
        return redirect("user_management")

    activation_sent = bool(getattr(settings, "LEGALTRACK_SEND_EMAILS", True))
    show_activation_link = bool(getattr(settings, "LEGALTRACK_SHOW_ACTIVATION_LINK", False))

    AuditLog.objects.create(
        actor=request.user,
        action="activation_email_sent",
        target_user=target,
        target_object=f"User: {target.email}",
        details={"resend": True}
    )

    if activation_sent:
        if show_activation_link:
            messages.success(
                request,
                format_html(
                    'Activation email resent. Dev link: <a href="{0}">{0}</a>',
                    activation_link,
                ),
            )
        else:
            messages.success(request, "Activation email resent.")
    else:
        messages.success(
            request,
            format_html(
                'Activation email is disabled in this environment. Copy this activation link: <a href="{0}">{0}</a>',
                activation_link,
            ),
        )
    return redirect("user_management")


@login_required
def set_password_view(request):
    if request.method == "POST":
        form = SetPasswordForm(request.user, request.POST)
        if form.is_valid():
            now = timezone.now()
            # Reset monthly count if it's a new month
            if request.user.last_password_change_at and request.user.last_password_change_at.month != now.month:
                request.user.password_change_count_this_month = 0
            
            if request.user.role != "super_admin":
                if request.user.password_change_count_this_month >= 2:
                    messages.error(request, "Password change limit (twice a month) reached. Contact the Super Admin for approval.")
                    return redirect("dashboard")

            form.save()
            request.user.must_change_password = False
            request.user.password_change_count_this_month += 1
            request.user.last_password_change_at = now
            request.user.save(update_fields=["must_change_password", "password_change_count_this_month", "last_password_change_at"])
            update_session_auth_hash(request, request.user)

            AuditLog.objects.create(
                actor=request.user,
                action="reset_password",
                target_object=f"User: {request.user.email}",
                details={"forced_reset": True, "count_this_month": request.user.password_change_count_this_month}
            )

            messages.success(request, "Password updated.")
            return redirect("dashboard")
    else:
        form = SetPasswordForm(request.user)

    return render(request, "core/set_password.html", {
        "role_display": request.user.get_role_display(),
        "form": form,
    })


def forgot_password(request):
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        user = CustomUser.objects.filter(email=email).first()
        if user:
            code = "".join(secrets.choice(string.digits) for _ in range(6))
            user.password_reset_code = code
            user.password_reset_code_created_at = timezone.now()
            user.save(update_fields=["password_reset_code", "password_reset_code_created_at"])
            
            subject = "PAStrack Password Reset Code"
            message = f"Your password reset code is: {code}\n\nThis code will expire in 15 minutes."
            
            try:
                send_mail(
                    subject,
                    message,
                    settings.DEFAULT_FROM_EMAIL,
                    [user.email],
                    fail_silently=False,
                )
                request.session["reset_email"] = user.email
                messages.success(request, "Reset code sent to your email.")
                return redirect("verify_reset_code")
            except Exception as e:
                import sys
                print(f"SMTP ERROR: {e}", file=sys.stderr)
                messages.error(request, "Failed to send reset email. Please contact the administrator.")
        else:
            messages.error(request, "No user found with that email.")
    return render(request, "registration/forgot_password.html")


def verify_reset_code(request):
    email = request.session.get("reset_email")
    if not email:
        return redirect("forgot_password")
        
    if request.method == "POST":
        code = request.POST.get("code", "").strip()
        user = CustomUser.objects.filter(email=email).first()
        if user and user.password_reset_code == code:
            # Check expiration (15 mins)
            if user.password_reset_code_created_at and (timezone.now() - user.password_reset_code_created_at) < timedelta(minutes=15):
                request.session["code_verified"] = True
                return redirect("reset_password_final")
            else:
                messages.error(request, "Code has expired.")
        else:
            messages.error(request, "Invalid code.")
            
    return render(request, "registration/verify_reset_code.html", {"email": email})


def reset_password_final(request):
    email = request.session.get("reset_email")
    verified = request.session.get("code_verified")
    if not email or not verified:
        return redirect("forgot_password")
        
    user = CustomUser.objects.filter(email=email).first()
    if not user:
        return redirect("forgot_password")
        
    if request.method == "POST":
        form = SetPasswordForm(user, request.POST)
        if form.is_valid():
            form.save()
            user.password_reset_code = ""
            user.save(update_fields=["password_reset_code"])
            
            del request.session["reset_email"]
            del request.session["code_verified"]
            
            messages.success(request, "Password has been reset. You can now log in.")
            return redirect("login")
    else:
        form = SetPasswordForm(user)
        
    return render(request, "registration/reset_password_final.html", {"form": form})


@login_required
def profile(request):
    if request.method == "POST":
        form = ProfileUpdateForm(request.POST, instance=request.user, user=request.user)
        if form.is_valid():
            form.save()
            AuditLog.objects.create(
                actor=request.user,
                action="update_user",
                target_user=request.user,
                target_object=f"User: {request.user.email}",
                details={"self_service": True},
            )
            messages.success(request, "Profile updated.")
            return redirect("profile")
    else:
        form = ProfileUpdateForm(
            instance=request.user,
            user=request.user,
            initial={"email_verify": request.user.email},
        )

    return render(request, "core/profile.html", {
        "role_display": request.user.get_role_display(),
        "form": form,
    })


def _lgu_owns_case(user, case: Case) -> bool:
    if getattr(user, "role", None) == "capitol_receiving":
        return getattr(case, "submitted_by_id", None) == user.id
    if getattr(user, "role", None) != "lgu_admin":
        return False
    user_mun = (getattr(user, "lgu_municipality", "") or "").strip()
    case_mun = (getattr(getattr(case, "submitted_by", None), "lgu_municipality", "") or "").strip()
    return bool(user_mun and case_mun and user_mun == case_mun)

def _lgu_can_edit_details(user, case: Case) -> bool:
    if not _lgu_owns_case(user, case):
        return False
    if case.status in {"draft", "not_received", "returned"}:
        return True
    if case.status == "client_correction":
        deadline = getattr(case, "client_correction_deadline", None)
        if deadline and timezone.now() > deadline:
            return False
        return True
    return False

def _lgu_can_edit_documents(user, case: Case) -> bool:
    if not _lgu_can_edit_details(user, case):
        return False
    if case.status == "returned":
        return True
    if case.status == "client_correction":
        return True
    if case.lgu_submitted_at is None:
        return True
    return False

def _lgu_can_finalize(user, case: Case) -> bool:
    return _lgu_can_edit_details(user, case) and (
        case.lgu_submitted_at is None or case.status in {"returned", "client_correction"}
    )

def _required_documents_missing(case: Case) -> list[str]:
    # Checklist items are informational only (nothing is required).
    return []


def _ensure_checklist_item(case: Case, *, doc_type: str, required: bool) -> None:
    items = list(case.checklist or [])
    for item in items:
        if isinstance(item, dict) and (item.get("doc_type") == doc_type):
            item["required"] = False
            item["uploaded"] = CaseDocument.objects.filter(case=case, doc_type=doc_type).exists()
            case.checklist = items
            case.save(update_fields=["checklist", "updated_at"])
            return

    items.insert(0, {
        "doc_type": doc_type,
        "required": False,
        "uploaded": CaseDocument.objects.filter(case=case, doc_type=doc_type).exists(),
    })
    case.checklist = items
    case.save(update_fields=["checklist", "updated_at"])


def _upsert_case_document(*, case: Case, doc_type: str, uploaded_file, actor: CustomUser | None):
    doc_type = (doc_type or "").strip()
    if not doc_type or not uploaded_file:
        return None

    doc, created = CaseDocument.objects.get_or_create(
        case=case,
        doc_type=doc_type,
        defaults={"uploaded_by": actor},
    )
    if not created and doc.file:
        with contextlib.suppress(Exception):
            doc.file.delete(save=False)
    doc.file = uploaded_file
    doc.uploaded_by = actor
    doc.save(update_fields=["file", "uploaded_by", "updated_at"])
    return doc


def _reset_case_uploads_and_checklist(*, case: Case) -> None:
    docs = list(CaseDocument.objects.filter(case=case).only("id", "file"))
    for d in docs:
        if getattr(d, "file", None):
            with contextlib.suppress(Exception):
                d.file.delete(save=False)
    CaseDocument.objects.filter(case=case).delete()
    case.checklist = []
    case.save(update_fields=["checklist", "updated_at"])


def _seed_case_checklist(*, case: Case) -> None:
    requirements = ["Endorsement Letter", *_case_type_requirements(
        getattr(case, "case_type", ""),
        title_type=getattr(case, "property_title_type", ""),
    )]
    seen = set()
    seeded = []
    for r in requirements:
        r = (r or "").strip()
        if not r:
            continue
        k = r.lower()
        if k in seen:
            continue
        seen.add(k)
        seeded.append({"doc_type": r, "required": False, "uploaded": False})
    case.checklist = seeded
    case.save(update_fields=["checklist", "updated_at"])

@login_required
@never_cache
def submit_case(request):
    if request.user.role not in {"lgu_admin", "capitol_receiving"}:
        messages.error(request, "Only LGU Admins and Receiver can create a new request.")
        return redirect("dashboard")

    if request.method == "POST":
        form = CaseDetailsForm(request.POST, request.FILES)
        if form.is_valid():
            cleaned = form.cleaned_data

            wants_save_draft = "save_draft" in request.POST
            wants_continue = "save_continue" in request.POST or not wants_save_draft

            # Prevent accidental duplicate drafts (e.g., browser back + re-submit).
            recent_window = timezone.now() - timedelta(minutes=2)
            existing = (
                Case.objects.filter(
                    submitted_by=request.user,
                    created_at__gte=recent_window,
                    lgu_submitted_at__isnull=True,
                    status__in=["draft", "not_received", "returned"],
                    client_first_name=(cleaned.get("client_first_name") or "").strip(),
                    client_last_name=(cleaned.get("client_last_name") or "").strip(),
                    client_middle_name=(cleaned.get("client_middle_name") or "").strip(),
                    client_suffix=(cleaned.get("client_suffix") or "").strip(),
                    case_type=(cleaned.get("case_type") or ""),
                )
                .order_by("-created_at")
                .first()
            )

            if existing:
                case = existing
                old_case_type = (case.case_type or "").strip()
                old_title_type = (case.property_title_type or "").strip()
                for field in CaseDetailsForm.Meta.fields:
                    setattr(case, field, cleaned.get(field))
                if case.status != "draft":
                    case.status = "draft"
                case.lgu_submitted_at = None
                case.save(update_fields=[*CaseDetailsForm.Meta.fields, "status", "lgu_submitted_at", "updated_at"])
                new_case_type = (case.case_type or "").strip()
                new_title_type = (case.property_title_type or "").strip()
                if (new_case_type != old_case_type) or (new_title_type != old_title_type):
                    _reset_case_uploads_and_checklist(case=case)
                    _seed_case_checklist(case=case)
                messages.info(request, "Continuing your existing draft.")
                if wants_save_draft and not wants_continue:
                    return redirect("drafts")
                return redirect("draft_wizard", draft_id=case.draft_id, step=2)

            case = form.save(commit=False)
            case.submitted_by = request.user
            case.status = "draft"
            case.lgu_submitted_at = None
            
            # Use selected area for prefix if available, fallback to user's municipality
            effective_mun = (case.area or "").strip()
            if not effective_mun:
                effective_mun = getattr(request.user, "lgu_municipality", "")
            
            case.lgu_area_code = _municipality_area_code(effective_mun)
            case.save()

            # Seed checklist suggestions (uploads happen in Step 2 only).
            requirements = ["Endorsement Letter", *_case_type_requirements(
                getattr(case, "case_type", ""),
                title_type=getattr(case, "property_title_type", ""),
            )]
            seen = set()
            seeded = []
            for r in requirements:
                r = (r or "").strip()
                if not r:
                    continue
                k = r.lower()
                if k in seen:
                    continue
                seen.add(k)
                seeded.append({"doc_type": r, "required": False, "uploaded": False})
            if seeded:
                case.checklist = seeded
                case.save(update_fields=["checklist", "updated_at"])

            AuditLog.objects.create(
                actor=request.user,
                action="case_create",
                target_object=f"Draft: {case.draft_id}",
                details={"client": case.client_name, "case_type": case.case_type}
            )

            if wants_save_draft and not wants_continue:
                messages.success(request, "Draft saved.")
                return redirect("drafts")

            messages.success(request, "Draft saved. Continue uploading documents.")
            return redirect("draft_wizard", draft_id=case.draft_id, step=2)
    else:
        form = CaseDetailsForm()

    return render(request, "core/submit_case.html", {
        "step": 1,
        "form": form,
        "case": None,
        "is_edit": False,
    })


@login_required
def edit_case(request, tracking_id):
    case = get_object_or_404(Case, tracking_id=tracking_id)
    if not _lgu_can_edit_details(request.user, case):
        messages.error(request, "You cannot edit this case.")
        return redirect("case_detail", tracking_id=case.tracking_id)
    return redirect("case_wizard", tracking_id=case.tracking_id, step=1)


@login_required
def case_wizard(request, tracking_id, step: int):
    case = get_object_or_404(Case, tracking_id=tracking_id)

    if request.user.role not in {"lgu_admin", "capitol_receiving"}:
        messages.error(request, "Only LGU Admins and Receiver can edit submissions.")
        return redirect("dashboard")

    if not _lgu_can_edit_details(request.user, case):
        messages.error(request, "This case can no longer be edited.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    step = int(step or 1)
    if step not in (1, 2, 3):
        return redirect("case_wizard", tracking_id=case.tracking_id, step=1)

    if step == 1:
        if request.method == "POST":
            old_case_type = (case.case_type or "").strip()
            old_title_type = (case.property_title_type or "").strip()
            form = CaseDetailsForm(request.POST, request.FILES, instance=case)
            if form.is_valid():
                updated = form.save(commit=False)
                
                # Update prefix based on selected area
                effective_mun = (updated.area or "").strip()
                if not effective_mun:
                    effective_mun = getattr(request.user, "lgu_municipality", "")
                updated.lgu_area_code = _municipality_area_code(effective_mun)
                
                updated.save()
                
                new_case_type = (updated.case_type or "").strip()
                new_title_type = (updated.property_title_type or "").strip()
                if (new_case_type != old_case_type) or (new_title_type != old_title_type):
                    _reset_case_uploads_and_checklist(case=updated)
                    _seed_case_checklist(case=updated)

                AuditLog.objects.create(
                    actor=request.user,
                    action="case_update",
                    target_object=f"Case: {case.tracking_id}",
                    details={"step": 1}
                )
                messages.success(request, "Details saved.")
                return redirect("case_wizard", tracking_id=case.tracking_id, step=2)
        else:
            form = CaseDetailsForm(instance=case)

        return render(request, "core/submit_case.html", {
            "step": 1,
            "form": form,
            "case": case,
            "is_edit": True,
            "documents": list(case.documents.all()),
        })

    if step == 2:
        if not _lgu_can_edit_documents(request.user, case):
            messages.error(request, "Document uploads can only be changed after the case is returned by Capitol Receiving.")
            return redirect("case_detail", tracking_id=case.tracking_id)

        requirements = ["Endorsement Letter", *_case_type_requirements(
            getattr(case, "case_type", ""),
            title_type=getattr(case, "property_title_type", ""),
        )]
        existing_checklist_types = [
            (i.get("doc_type") or "").strip()
            for i in (case.checklist or [])
            if isinstance(i, dict)
        ]
        existing_doc_types = [d.doc_type for d in case.documents.all()]
        doc_type_choices = list(dict.fromkeys([
            *requirements,
            *existing_checklist_types,
            *existing_doc_types,
            "Endorsement Letter",
        ]))

        FormSet = forms.formset_factory(ChecklistItemForm, extra=0)

        initial = []
        if case.checklist:
            for item in (case.checklist or []):
                if isinstance(item, dict):
                    initial.append({
                        "doc_type": item.get("doc_type", ""),
                        "required": False,
                    })
        else:
            for req in requirements:
                initial.append({"doc_type": req, "required": False})

        if request.method == "POST":
            if "add_row" in request.POST:
                data = request.POST.copy()
                try:
                    total = int(data.get("form-TOTAL_FORMS") or "0")
                except ValueError:
                    total = 0
                data["form-TOTAL_FORMS"] = str(total + 1)
                formset = FormSet(data, request.FILES, form_kwargs={"doc_type_choices": doc_type_choices})
                docs = list(case.documents.all())
                return render(request, "core/submit_case.html", {
                    "step": 2,
                    "formset": formset,
                    "case": case,
                    "is_edit": True,
                    "documents": docs,
                    "documents_by_type": {d.doc_type: d for d in docs},
                    "rows": _build_checklist_rows(formset, docs),
                    "case_type_requirements": requirements,
                })

            formset = FormSet(request.POST, request.FILES, form_kwargs={"doc_type_choices": doc_type_choices})
            if formset.is_valid():
                new_checklist = []
                seen = set()

                for f in formset:
                    cd = f.cleaned_data
                    if not cd:
                        continue

                    doc_type = (cd.get("doc_type") or "").strip()
                    if not doc_type:
                        continue

                    key = doc_type.lower()
                    if key in seen:
                        messages.error(request, f"Duplicate document type: {doc_type}")
                        docs = list(case.documents.all())
                        return render(request, "core/submit_case.html", {
                            "step": 2,
                            "formset": formset,
                            "case": case,
                            "is_edit": True,
                            "documents": docs,
                            "documents_by_type": {d.doc_type: d for d in docs},
                            "rows": _build_checklist_rows(formset, docs),
                            "case_type_requirements": requirements,
                        })
                    seen.add(key)

                    uploaded_file = cd.get("file")
                    if uploaded_file:
                        _upsert_case_document(case=case, doc_type=doc_type, uploaded_file=uploaded_file, actor=request.user)

                    has_doc = CaseDocument.objects.filter(case=case, doc_type=doc_type).exists()
                    new_checklist.append({
                        "doc_type": doc_type,
                        "required": False,
                        "uploaded": bool(has_doc),
                    })

                if CaseDocument.objects.filter(case=case, doc_type="Endorsement Letter").exists():
                    if not any((i.get("doc_type") == "Endorsement Letter") for i in new_checklist):
                        new_checklist.insert(0, {"doc_type": "Endorsement Letter", "required": False, "uploaded": True})
                else:
                    if not any((i.get("doc_type") == "Endorsement Letter") for i in new_checklist):
                        new_checklist.insert(0, {"doc_type": "Endorsement Letter", "required": False, "uploaded": False})

                case.checklist = new_checklist
                if case.status in {"returned", "client_correction"}:
                    case.status = "not_received"
                    case.client_correction_deadline = None
                case.lgu_submitted_at = None
                case.save(update_fields=["checklist", "status", "client_correction_deadline", "updated_at", "lgu_submitted_at"])

                AuditLog.objects.create(
                    actor=request.user,
                    action="case_update",
                    target_object=f"Case: {case.tracking_id}",
                    details={"step": 2, "items": len(new_checklist)}
                )

                messages.success(request, "Checklist and uploads saved.")
                return redirect("case_wizard", tracking_id=case.tracking_id, step=3)
        else:
            formset = FormSet(initial=initial, form_kwargs={"doc_type_choices": doc_type_choices})

        docs = list(case.documents.all())

        return render(request, "core/submit_case.html", {
            "step": 2,
            "formset": formset,
            "case": case,
            "is_edit": True,
            "documents": docs,
            "documents_by_type": {d.doc_type: d for d in docs},
            "rows": _build_checklist_rows(formset, docs),
            "case_type_requirements": requirements,
        })

    # Wizard step 3
    if not _lgu_can_finalize(request.user, case):
        messages.error(request, "This case cannot be finalized right now.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    checklist = []
    for item in (case.checklist or []):
        if not isinstance(item, dict):
            continue
        doc_type = (item.get("doc_type") or "").strip()
        if not doc_type:
            continue
        checklist.append({
            "doc_type": doc_type,
            "required": False,
            "uploaded": CaseDocument.objects.filter(case=case, doc_type=doc_type).exists(),
        })

    if request.method == "POST":
        if case.status in {"returned", "client_correction"}:
            case.status = "not_received"
            case.client_correction_deadline = None

        case.lgu_submitted_at = timezone.now()
        
        # Priority: 1. case.area, 2. submitted_by.lgu_municipality
        effective_mun = (case.area or "").strip()
        if not effective_mun:
            effective_mun = getattr(getattr(case, "submitted_by", None), "lgu_municipality", "")
            
        if not (case.lgu_area_code or "").strip():
            case.lgu_area_code = _municipality_area_code(effective_mun)
        case.save(update_fields=["status", "client_correction_deadline", "lgu_area_code", "lgu_submitted_at", "updated_at"])

        AuditLog.objects.create(
            actor=request.user,
            action="case_update",
            target_object=f"Case: {case.tracking_id}",
            details={"step": 3, "finalized": True}
        )
        messages.success(request, f"Case {case.tracking_id} submitted.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    return render(request, "core/submit_case.html", {
        "step": 3,
        "case": case,
        "is_edit": True,
        "documents": list(case.documents.all()),
        "checklist": checklist,
    })


@login_required
def drafts(request):
    if request.user.role not in {"lgu_admin", "capitol_receiving"}:
        messages.error(request, "Not authorized.")
        return redirect("dashboard")

    qs = (
        Case.objects.filter(
            submitted_by=request.user,
            status="draft",
            lgu_submitted_at__isnull=True,
        )
        .order_by("-updated_at")
    )
    return render(request, "core/drafts.html", {"drafts": list(qs)})


@login_required
def draft_wizard(request, draft_id, step: int):
    case = get_object_or_404(Case, draft_id=draft_id)

    # If already submitted, go to the official case page.
    if case.tracking_id and case.lgu_submitted_at is not None:
        return redirect("case_detail", tracking_id=case.tracking_id)

    if request.user.role not in {"lgu_admin", "capitol_receiving"}:
        messages.error(request, "Only LGU Admins and Receiver can edit drafts.")
        return redirect("dashboard")

    if not _lgu_can_edit_details(request.user, case):
        messages.error(request, "This draft can no longer be edited.")
        return redirect("drafts")

    step = int(step or 1)
    if step not in (1, 2, 3):
        return redirect("draft_wizard", draft_id=case.draft_id, step=1)

    if step == 1:
        if request.method == "POST":
            old_case_type = (case.case_type or "").strip()
            old_title_type = (case.property_title_type or "").strip()
            form = CaseDetailsForm(request.POST, request.FILES, instance=case)
            if form.is_valid():
                case = form.save(commit=False)
                case.status = "draft"
                case.lgu_submitted_at = None
                if not (case.lgu_area_code or "").strip():
                    case.lgu_area_code = _municipality_area_code(getattr(getattr(case, "submitted_by", None), "lgu_municipality", ""))
                case.save()
                new_case_type = (case.case_type or "").strip()
                new_title_type = (case.property_title_type or "").strip()
                if (new_case_type != old_case_type) or (new_title_type != old_title_type):
                    _reset_case_uploads_and_checklist(case=case)
                    _seed_case_checklist(case=case)

                AuditLog.objects.create(
                    actor=request.user,
                    action="case_update",
                    target_object=f"Draft: {case.draft_id}",
                    details={"step": 1}
                )
                if "save_draft" in request.POST:
                    messages.success(request, "Draft saved.")
                    return redirect("drafts")

                messages.success(request, "Draft details saved.")
                return redirect("draft_wizard", draft_id=case.draft_id, step=2)
        else:
            form = CaseDetailsForm(instance=case)

        return render(request, "core/submit_case.html", {
            "step": 1,
            "form": form,
            "case": case,
            "is_edit": True,
            "documents": list(case.documents.all()),
        })

    if step == 2:
        if not _lgu_can_edit_documents(request.user, case):
            messages.error(request, "Document uploads can only be changed after the case is returned by Capitol Receiving.")
            return redirect("draft_wizard", draft_id=case.draft_id, step=1)

        requirements = ["Endorsement Letter", *_case_type_requirements(
            getattr(case, "case_type", ""),
            title_type=getattr(case, "property_title_type", ""),
        )]
        existing_checklist_types = [
            (i.get("doc_type") or "").strip()
            for i in (case.checklist or [])
            if isinstance(i, dict)
        ]
        existing_doc_types = [d.doc_type for d in case.documents.all()]
        doc_type_choices = list(dict.fromkeys([
            *requirements,
            *existing_checklist_types,
            *existing_doc_types,
            "Endorsement Letter",
        ]))

        FormSet = forms.formset_factory(ChecklistItemForm, extra=0)

        initial = []
        if case.checklist:
            for item in (case.checklist or []):
                if isinstance(item, dict):
                    initial.append({
                        "doc_type": item.get("doc_type", ""),
                        "required": False,
                    })
        else:
            for req in requirements:
                initial.append({"doc_type": req, "required": False})

        if request.method == "POST":
            if "add_row" in request.POST:
                data = request.POST.copy()
                try:
                    total = int(data.get("form-TOTAL_FORMS") or "0")
                except ValueError:
                    total = 0
                data["form-TOTAL_FORMS"] = str(total + 1)
                formset = FormSet(data, request.FILES, form_kwargs={"doc_type_choices": doc_type_choices})
                docs = list(case.documents.all())
                return render(request, "core/submit_case.html", {
                    "step": 2,
                    "formset": formset,
                    "case": case,
                    "is_edit": True,
                    "documents": docs,
                    "documents_by_type": {d.doc_type: d for d in docs},
                    "rows": _build_checklist_rows(formset, docs),
                    "case_type_requirements": requirements,
                })

            formset = FormSet(request.POST, request.FILES, form_kwargs={"doc_type_choices": doc_type_choices})
            if formset.is_valid():
                new_checklist = []
                seen = set()

                for f in formset:
                    cd = f.cleaned_data
                    if not cd:
                        continue

                    doc_type = (cd.get("doc_type") or "").strip()
                    if not doc_type:
                        continue

                    key = doc_type.lower()
                    if key in seen:
                        messages.error(request, f"Duplicate document type: {doc_type}")
                        docs = list(case.documents.all())
                        return render(request, "core/submit_case.html", {
                            "step": 2,
                            "formset": formset,
                            "case": case,
                            "is_edit": True,
                            "documents": docs,
                            "documents_by_type": {d.doc_type: d for d in docs},
                            "rows": _build_checklist_rows(formset, docs),
                            "case_type_requirements": requirements,
                        })
                    seen.add(key)

                    uploaded_file = cd.get("file")
                    if uploaded_file:
                        _upsert_case_document(case=case, doc_type=doc_type, uploaded_file=uploaded_file, actor=request.user)

                    has_doc = CaseDocument.objects.filter(case=case, doc_type=doc_type).exists()
                    new_checklist.append({
                        "doc_type": doc_type,
                        "required": False,
                        "uploaded": bool(has_doc),
                    })

                if CaseDocument.objects.filter(case=case, doc_type="Endorsement Letter").exists():
                    if not any((i.get("doc_type") == "Endorsement Letter") for i in new_checklist):
                        new_checklist.insert(0, {"doc_type": "Endorsement Letter", "required": False, "uploaded": True})
                else:
                    if not any((i.get("doc_type") == "Endorsement Letter") for i in new_checklist):
                        new_checklist.insert(0, {"doc_type": "Endorsement Letter", "required": False, "uploaded": False})

                case.checklist = new_checklist
                case.status = "draft"
                case.lgu_submitted_at = None
                case.save(update_fields=["checklist", "status", "updated_at", "lgu_submitted_at"])

                AuditLog.objects.create(
                    actor=request.user,
                    action="case_update",
                    target_object=f"Draft: {case.draft_id}",
                    details={"step": 2, "items": len(new_checklist)}
                )

                if "save_draft" in request.POST:
                    messages.success(request, "Draft saved.")
                    return redirect("drafts")

                messages.success(request, "Draft checklist and uploads saved.")
                return redirect("draft_wizard", draft_id=case.draft_id, step=3)
        else:
            formset = FormSet(initial=initial, form_kwargs={"doc_type_choices": doc_type_choices})

        docs = list(case.documents.all())

        return render(request, "core/submit_case.html", {
            "step": 2,
            "formset": formset,
            "case": case,
            "is_edit": True,
            "documents": docs,
            "documents_by_type": {d.doc_type: d for d in docs},
            "rows": _build_checklist_rows(formset, docs),
            "case_type_requirements": requirements,
        })

    # Wizard step 3
    if not _lgu_can_finalize(request.user, case):
        messages.error(request, "This draft cannot be submitted right now.")
        return redirect("draft_wizard", draft_id=case.draft_id, step=1)

    checklist = []
    for item in (case.checklist or []):
        if not isinstance(item, dict):
            continue
        doc_type = (item.get("doc_type") or "").strip()
        if not doc_type:
            continue
        checklist.append({
            "doc_type": doc_type,
            "required": False,
            "uploaded": CaseDocument.objects.filter(case=case, doc_type=doc_type).exists(),
        })

    if request.method == "POST":
        if "save_draft" in request.POST:
            messages.success(request, "Draft saved.")
            return redirect("drafts")

        case.status = "not_received"
        case.lgu_submitted_at = timezone.now()

        # Priority: 1. case.area, 2. submitted_by.lgu_municipality
        effective_mun = (case.area or "").strip()
        if not effective_mun:
            effective_mun = getattr(getattr(case, "submitted_by", None), "lgu_municipality", "")

        if not (case.lgu_area_code or "").strip():
            case.lgu_area_code = _municipality_area_code(effective_mun)
        case.save(update_fields=["status", "lgu_area_code", "lgu_submitted_at", "updated_at", "tracking_id"])

        AuditLog.objects.create(
            actor=request.user,
            action="case_update",
            target_object=f"Case: {case.tracking_id}",
            details={"step": 3, "finalized": True}
        )
        messages.success(request, f"Case {case.tracking_id} submitted.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    return render(request, "core/submit_case.html", {
        "step": 3,
        "case": case,
        "is_edit": True,
        "documents": list(case.documents.all()),
        "checklist": checklist,
    })


@login_required
@require_POST
def delete_draft(request, draft_id):
    if request.user.role not in {"lgu_admin", "capitol_receiving"}:
        messages.error(request, "Not authorized.")
        return redirect("dashboard")

    case = get_object_or_404(
        Case,
        draft_id=draft_id,
        submitted_by=request.user,
        status="draft",
        lgu_submitted_at__isnull=True,
    )

    case.delete()
    messages.success(request, "Draft deleted.")
    return redirect("drafts")


@login_required
def case_detail(request, tracking_id):
    case = get_object_or_404(Case, tracking_id=tracking_id)

    # Prevent LGU users (and any non-capitol role) from viewing cases they don't own.
    if not _user_can_view_case(request.user, case):
        raise Http404()

    can_edit = _lgu_can_edit_details(request.user, case)

    can_receive = (
        request.user.role == "capitol_receiving" and
        case.status in {"not_received"}
    )

    can_return = (
        request.user.role == "capitol_receiving" and
        case.status in {"not_received", "received"} and
        case.assigned_to_id is None
    )

    can_assign = (
        request.user.role == "capitol_receiving" and
        case.status == "received" and
        case.assigned_to_id is None
    )

    can_submit_for_approval = (
        request.user.role == "capitol_examiner" and
        case.status in {"for_review", "under_review", "in_review"} and
        case.assigned_to_id == request.user.id
    )

    can_return_to_receiving = (
        request.user.role == "capitol_examiner" and
        case.status in {"for_review", "under_review", "in_review"} and
        case.assigned_to_id == request.user.id
    )

    can_approve = (
        request.user.role == "capitol_approver" and
        case.status == "for_approval"
    )

    can_assign_taxmapper = bool(
        request.user.role == "capitol_approver"
        and case.status == "for_approval"
        and bool(getattr(case, "needs_taxmapping", False))
    )

    can_number = (
        request.user.role == "capitol_numberer" and
        case.status == "for_numbering"
    )

    can_complete_taxmapping = bool(
        request.user.role == "capitol_taxmapper"
        and case.status == "for_taxmapping"
        and getattr(case, "taxmapper_assigned_to_id", None) == getattr(request.user, "id", None)
    )

    can_release = (
        request.user.role == "capitol_releaser" and
        case.status == "for_release"
    )

    examiners = None
    if can_assign:
        examiners = (
            CustomUser.objects.filter(role="capitol_examiner", is_active=True)
            .annotate(active_load=Count("assigned_cases", filter=Q(assigned_cases__status="in_review")))
            .order_by("active_load", "full_name", "email")
        )

    def _is_owner_for_internal_sections(user: CustomUser, case: Case) -> bool:
        role = getattr(user, "role", "") or ""
        if role == "super_admin":
            return True
        if role == "capitol_receiving":
            return case.status in {"not_received", "received"} and case.assigned_to_id is None
        if role == "capitol_examiner":
            return case.status in {"for_review", "under_review", "in_review"} and case.assigned_to_id == user.id
        if role == "capitol_approver":
            return case.status == "for_approval"
        if role == "capitol_taxmapper":
            return case.status == "for_taxmapping" and case.taxmapper_assigned_to_id == user.id
        if role == "capitol_numberer":
            return case.status == "for_numbering"
        if role == "capitol_releaser":
            return case.status == "for_release"
        return False

    is_capitol = bool(request.user.is_authenticated and (_is_capitol_staff(request.user) or request.user.role == "super_admin"))
    show_internal = bool(is_capitol and case.status != "client_correction" and _is_owner_for_internal_sections(request.user, case))

    remarks = []
    history = []
    remark_form = None
    can_remark = False

    if show_internal:
        remarks_qs = CaseRemark.objects.filter(case=case).select_related("created_by")
        history_qs = (
            AuditLog.objects.filter(target_object=f"Case: {case.tracking_id}")
            .filter(action__in=["case_create", "case_receipt", "case_assignment", "case_status_change", "case_approval", "case_rejection", "case_release"])
            .select_related("actor")
            .order_by("-created_at")
        )

        history = list(history_qs)
        for h in history:
            h.details_display = _format_case_history_details(getattr(h, "action", "") or "", getattr(h, "details", None))

        remarks = list(remarks_qs)

        # Remarks are allowed only by the current responsible actor (owner).
        can_remark = True
        remark_form = CaseRemarkForm()

    case_numbers = list(CaseNumber.objects.filter(case=case).order_by("number").values_list("number", flat=True))
    last_used_number = CaseNumber.objects.order_by("-number").values_list("number", flat=True).first()
    suggested_next_number = str(((int(last_used_number) + 1) if (last_used_number and str(last_used_number).isdigit()) else 1)).zfill(5)

    taxmappers = None
    if can_assign_taxmapper:
        taxmappers = CustomUser.objects.filter(role="capitol_taxmapper", is_active=True).order_by("full_name", "email")

    response_context = {
        "case": case,
        "documents": list(case.documents.all()),
        "can_edit": can_edit,
        "can_receive": can_receive,
        "can_return": can_return,
        "can_assign": can_assign,
        "can_submit_for_approval": can_submit_for_approval,
        "can_return_to_receiving": can_return_to_receiving,
        "can_approve": can_approve,
        "can_assign_taxmapper": can_assign_taxmapper,
        "taxmappers": taxmappers,
        "can_complete_taxmapping": can_complete_taxmapping,
        "can_number": can_number,
        "can_release": can_release,
        "examiners": examiners,
        "case_numbers": case_numbers,
        "last_used_number": last_used_number,
        "suggested_next_number": suggested_next_number,
        "show_internal": show_internal,
        "remarks": remarks,
        "history": history,
        "can_remark": can_remark,
        "remark_form": remark_form,
    }

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render(request, "core/case_detail_drawer.html", response_context)

    return render(request, "core/case_detail.html", response_context)


@login_required
@require_POST
def add_case_remark(request, tracking_id):
    case = get_object_or_404(Case, tracking_id=tracking_id)
    role = getattr(request.user, "role", "") or ""
    if role == "super_admin":
        pass
    elif role == "capitol_receiving":
        if not (case.status in {"not_received", "received"} and case.assigned_to_id is None and case.status != "client_correction"):
            messages.error(request, "Not authorized to remark on this case right now.")
            return redirect("case_detail", tracking_id=case.tracking_id)
    elif role == "capitol_examiner":
        if not (case.status == "in_review" and case.assigned_to_id == request.user.id and case.status != "client_correction"):
            messages.error(request, "Not authorized to remark on this case right now.")
            return redirect("case_detail", tracking_id=case.tracking_id)
    elif role == "capitol_approver":
        if not (case.status == "for_approval" and case.status != "client_correction"):
            messages.error(request, "Not authorized to remark on this case right now.")
            return redirect("case_detail", tracking_id=case.tracking_id)
    elif role == "capitol_taxmapper":
        if not (case.status == "for_taxmapping" and case.taxmapper_assigned_to_id == request.user.id and case.status != "client_correction"):
            messages.error(request, "Not authorized to remark on this case right now.")
            return redirect("case_detail", tracking_id=case.tracking_id)
    elif role == "capitol_numberer":
        if not (case.status == "for_numbering" and case.status != "client_correction"):
            messages.error(request, "Not authorized to remark on this case right now.")
            return redirect("case_detail", tracking_id=case.tracking_id)
    elif role == "capitol_releaser":
        if not (case.status == "for_release" and case.status != "client_correction"):
            messages.error(request, "Not authorized to remark on this case right now.")
            return redirect("case_detail", tracking_id=case.tracking_id)
    else:
        messages.error(request, "Not authorized.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    form = CaseRemarkForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Please enter a valid remark.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    text = form.cleaned_data["text"]
    CaseRemark.objects.create(case=case, text=text, created_by=request.user)

    AuditLog.objects.create(
        actor=request.user,
        action="case_remark",
        target_object=f"Case: {case.tracking_id}",
        details={"text": text[:2000]},
    )

    messages.success(request, "Remark added.")
    return redirect("case_detail", tracking_id=case.tracking_id)


@login_required
def submissions(request):
    if not (_is_capitol_staff(request.user) or request.user.role == "super_admin"):
        messages.error(request, "Not authorized.")
        return redirect("dashboard")

    tab = (request.GET.get("tab") or "").strip().lower() or "all"
    q = (request.GET.get("q") or "").strip()
    case_type = (request.GET.get("case_type") or "").strip()
    lgu = (request.GET.get("lgu") or "").strip()
    date_from_raw = (request.GET.get("date_from") or "").strip()
    date_to_raw = (request.GET.get("date_to") or "").strip()

    date_from = parse_date(date_from_raw) if date_from_raw else None
    date_to = parse_date(date_to_raw) if date_to_raw else None

    qs = Case.objects.filter(lgu_submitted_at__isnull=False).select_related("submitted_by", "assigned_to").order_by("-created_at")

    if request.user.role == "capitol_examiner":
        qs = qs.filter(Q(assigned_to=request.user) | Q(status__in=["for_approval", "for_taxmapping", "for_numbering", "for_release", "released"]))
    elif request.user.role == "capitol_approver":
        qs = qs.filter(status__in=["for_approval", "for_taxmapping", "for_numbering", "for_release", "released"])
    elif request.user.role == "capitol_taxmapper":
        qs = qs.filter(Q(status="for_taxmapping", taxmapper_assigned_to=request.user) | Q(status__in=["for_approval", "for_numbering", "for_release", "released"]))
    elif request.user.role == "capitol_numberer":
        qs = qs.filter(status__in=["for_numbering", "for_release", "released"])
    elif request.user.role == "capitol_releaser":
        qs = qs.filter(status__in=["for_release", "released"])

    tab_map = {
        "all": None,
        "pending": {"not_received", "client_correction"},
        "received": {"received"},
        "in_review": {"in_review", "for_review", "under_review"},
        "for_taxmapping": {"for_taxmapping"},
        "for_approval": {"for_approval"},
        "for_numbering": {"for_numbering"},
        "for_release": {"for_release"},
        "released": {"released"},
    }
    statuses = tab_map.get(tab)
    if statuses:
        qs = qs.filter(status__in=statuses)

    if q:
        qs = qs.filter(
            Q(tracking_id__icontains=q) |
            Q(client_name__icontains=q) |
            Q(client_first_name__icontains=q) |
            Q(client_last_name__icontains=q) |
            Q(client_middle_name__icontains=q) |
            Q(client_email__icontains=q) |
            Q(client_number__icontains=q) |
            Q(client_contact__icontains=q) |
            Q(submitted_by__email__icontains=q)
        )

    if case_type:
        qs = qs.filter(case_type=case_type)
    if lgu:
        qs = qs.filter(submitted_by__lgu_municipality=lgu)
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)

    number_q = (request.GET.get("number") or "").strip()
    if number_q:
        if number_q.isdigit():
            padded = number_q.zfill(5) if len(number_q) <= 5 else number_q
            qs = qs.filter(Q(numbers__number=padded) | Q(tracking_id__icontains=number_q)).distinct()
        else:
            qs = qs.filter(Q(tracking_id__icontains=number_q))

    query = request.GET.copy()
    with contextlib.suppress(Exception):
        query.pop("page")

    query_no_tab = request.GET.copy()
    with contextlib.suppress(Exception):
        query_no_tab.pop("page")
        query_no_tab.pop("tab")

    paginator = Paginator(qs, 15)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    tabs = [
        ("all", "All"),
        ("pending", "Pending"),
        ("received", "Received"),
        ("in_review", "In Review"),
        ("for_taxmapping", "For Taxmapping"),
        ("for_approval", "For Approval"),
        ("for_numbering", "For Numbering"),
        ("for_release", "For Release"),
        ("released", "Released"),
    ]

    return render(request, "core/submissions.html", {
        "role_display": request.user.get_role_display(),
        "page_obj": page_obj,
        "tab": tab,
        "q": q,
        "tabs": tabs,
        "filter_case_type": case_type,
        "filter_lgu": lgu,
        "filter_date_from": date_from_raw,
        "filter_date_to": date_to_raw,
        "case_type_choices": list(getattr(Case, "CASE_TYPE_CHOICES", [])),
        "lgu_choices": list(getattr(CustomUser, "LGU_MUNICIPALITY_CHOICES", [])),
        "qs_params": query.urlencode(),
        "qs_params_no_tab": query_no_tab.urlencode(),
    })


@login_required
@require_POST
def receive_case(request, tracking_id):
    case = get_object_or_404(Case, tracking_id=tracking_id)

    if request.user.role != "capitol_receiving":
        messages.error(request, "Only Capitol Receiver can receive cases.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.status not in {"not_received"}:
        messages.error(request, "This case cannot be received in its current status.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    case.status = "received"
    case.received_at = timezone.now()
    case.received_by = request.user
    case.save()

    AuditLog.objects.create(
        actor=request.user,
        action="case_receipt",
        target_object=f"Case: {case.tracking_id}",
        details={"new_status": case.status}
    )

    send_case_email(
        to_email=(case.client_email or "").strip(),
        subject=f"PAStrack: Case {case.tracking_id} received",
        message=(
            f"Your request {case.tracking_id} has been marked as physically received.\n\n"
            f"Current status: {dict(Case.STATUS_CHOICES).get(case.status, case.status)}\n"
        ),
    )
    sns_hook(event="case_received", payload={"tracking_id": case.tracking_id, "status": case.status})

    messages.success(request, f"Case {case.tracking_id} marked as Received.")
    return redirect("case_detail", tracking_id=case.tracking_id)


@login_required
@require_POST
def return_case(request, tracking_id):
    case = get_object_or_404(Case, tracking_id=tracking_id)

    if request.user.role != "capitol_receiving":
        messages.error(request, "Only Receiver can return cases.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.status not in {"not_received", "received"}:
        messages.error(request, "Only pending/received cases can be returned to the client.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.assigned_to_id is not None:
        messages.error(request, "This case is assigned and cannot be returned right now.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "Return reason is required.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.documents.exists() and case.documents.filter(reviewed_ok=False, review_remark="").exists():
        messages.error(request, "Add remarks to unchecked documents before returning to the client.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    case.status = "client_correction"
    case.return_reason = reason
    case.returned_at = timezone.now()
    case.returned_by = request.user
    case.client_correction_deadline = timezone.now() + timedelta(days=30)
    case.save(update_fields=[
        "status",
        "return_reason",
        "returned_at",
        "returned_by",
        "client_correction_deadline",
        "updated_at",
    ])

    AuditLog.objects.create(
        actor=request.user,
        action="case_status_change",
        target_object=f"Case: {case.tracking_id}",
        details={"new_status": case.status, "reason": reason, "deadline": case.client_correction_deadline.isoformat() if case.client_correction_deadline else None}
    )

    email_ok = send_case_email(
        to_email=(case.client_email or "").strip(),
        subject=f"PAStrack: Action needed for case {case.tracking_id}",
        message=(
            f"Your request {case.tracking_id} was returned for correction.\n\n"
            f"Reason: {reason}\n"
            f"Correction deadline: {case.client_correction_deadline}\n"
        ),
    )
    phone = (case.client_number or "").strip()
    sns_ok = False
    if phone:
        sns_ok = sns_hook(event="case_returned_to_client", payload={
            "tracking_id": case.tracking_id,
            "status": case.status,
            "deadline": case.client_correction_deadline.isoformat() if case.client_correction_deadline else None,
            "phone": phone,
        })
    if email_ok or sns_ok:
        messages.info(request, "Client notification sent.")

    messages.success(request, f"Case {case.tracking_id} returned to client (30-day correction window).")
    return redirect("case_detail", tracking_id=case.tracking_id)


@login_required
@require_POST
def assign_case(request, tracking_id):
    case = get_object_or_404(Case, tracking_id=tracking_id)

    if request.user.role != "capitol_receiving":
        messages.error(request, "Only Receiver can assign cases.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.status != "received" or case.assigned_to_id is not None:
        messages.error(request, "This case is not eligible for assignment.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    examiner_id = request.POST.get("examiner_id")
    examiner = get_object_or_404(CustomUser, id=examiner_id, role="capitol_examiner", is_active=True)

    case.assigned_to = examiner
    case.assigned_at = timezone.now()
    case.status = "in_review"
    case.save()

    AuditLog.objects.create(
        actor=request.user,
        action="case_assignment",
        target_object=f"Case: {case.tracking_id}",
        details={
            "new_status": case.status,
            "assigned_to": f"{examiner.get_full_name()} - {examiner.get_role_display()}",
        }
    )

    messages.success(request, f"Case {case.tracking_id} assigned.")
    return redirect("case_detail", tracking_id=case.tracking_id)


@login_required
@require_POST
def submit_for_approval(request, tracking_id):
    case = get_object_or_404(Case, tracking_id=tracking_id)

    if request.user.role != "capitol_examiner":
        messages.error(request, "Only Examiners can submit cases for approval.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.status not in {"in_review", "for_review", "under_review"} or case.assigned_to_id != request.user.id:
        messages.error(request, "This case is not eligible for approval submission.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.documents.exists() and case.documents.filter(reviewed_ok=False).exists():
        messages.error(request, "Review all uploaded documents and mark them as checked before submitting for approval.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    old_status = case.status
    case.status = "for_approval"
    case.save(update_fields=["status", "updated_at"])

    AuditLog.objects.create(
        actor=request.user,
        action="case_status_change",
        target_object=f"Case: {case.tracking_id}",
        details={"old_status": old_status, "new_status": case.status}
    )

    messages.success(request, f"Case {case.tracking_id} sent for approval.")
    return redirect("case_detail", tracking_id=case.tracking_id)


@login_required
@require_POST
def approve_case(request, tracking_id):
    case = get_object_or_404(Case, tracking_id=tracking_id)

    if request.user.role != "capitol_approver":
        messages.error(request, "Only Approvers can approve cases.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.status != "for_approval":
        messages.error(request, "This case is not eligible for approval.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.documents.exists() and case.documents.filter(reviewed_ok=False).exists():
        messages.error(request, "Review all uploaded documents and mark them as checked before approving.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if getattr(case, "needs_taxmapping", False):
        messages.error(request, "This transaction requires tax mapping. Assign a Tax Mapper instead of approving.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    old_status = case.status
    case.status = "for_numbering"
    case.save(update_fields=["status", "updated_at"])

    AuditLog.objects.create(
        actor=request.user,
        action="case_approval",
        target_object=f"Case: {case.tracking_id}",
        details={"old_status": old_status, "new_status": case.status}
    )

    send_case_email(
        to_email=(case.client_email or "").strip(),
        subject=f"PAStrack: Case {case.tracking_id} approved",
        message=(
            f"Your request {case.tracking_id} has been approved.\n\n"
            f"Current status: {dict(Case.STATUS_CHOICES).get(case.status, case.status)}\n"
        ),
    )
    sns_hook(event="case_approved", payload={"tracking_id": case.tracking_id, "status": case.status})

    messages.success(request, f"Case {case.tracking_id} approved.")
    return redirect("case_detail", tracking_id=case.tracking_id)


@login_required
@require_POST
def assign_taxmapper(request, tracking_id):
    case = get_object_or_404(Case, tracking_id=tracking_id)

    if request.user.role != "capitol_approver":
        messages.error(request, "Only Approvers can assign Tax Mappers.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.status != "for_approval" or not getattr(case, "needs_taxmapping", False):
        messages.error(request, "This case is not eligible for tax mapping assignment.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    taxmapper_id = (request.POST.get("taxmapper_id") or "").strip()
    if not taxmapper_id.isdigit():
        messages.error(request, "Please select a Tax Mapper.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    taxmapper = get_object_or_404(CustomUser, id=int(taxmapper_id), role="capitol_taxmapper", is_active=True)

    old_status = case.status
    case.taxmapper_assigned_to = taxmapper
    case.taxmapped_at = None
    case.status = "for_taxmapping"
    case.save(update_fields=["taxmapper_assigned_to", "taxmapped_at", "status", "updated_at"])

    AuditLog.objects.create(
        actor=request.user,
        action="case_status_change",
        target_object=f"Case: {case.tracking_id}",
        details={
            "old_status": old_status,
            "new_status": case.status,
            "assigned_to": f"{taxmapper.get_full_name()} - {taxmapper.get_role_display()}",
        },
    )

    messages.success(request, f"Case {case.tracking_id} assigned for tax mapping.")
    return redirect("case_detail", tracking_id=case.tracking_id)


@login_required
@require_POST
def complete_taxmapping(request, tracking_id):
    case = get_object_or_404(Case, tracking_id=tracking_id)

    if request.user.role != "capitol_taxmapper":
        messages.error(request, "Only Tax Mappers can complete tax mapping.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.status != "for_taxmapping" or case.taxmapper_assigned_to_id != request.user.id:
        messages.error(request, "This case is not assigned to you for tax mapping.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    old_status = case.status
    case.taxmapped_at = timezone.now()
    case.status = "for_numbering"
    case.save(update_fields=["taxmapped_at", "status", "updated_at"])

    AuditLog.objects.create(
        actor=request.user,
        action="case_status_change",
        target_object=f"Case: {case.tracking_id}",
        details={"old_status": old_status, "new_status": case.status, "taxmapped": True},
    )

    messages.success(request, f"Case {case.tracking_id} marked as taxmapped and sent to Numberer.")
    return redirect("case_detail", tracking_id=case.tracking_id)


@login_required
@require_POST
def return_for_correction(request, tracking_id):
    case = get_object_or_404(Case, tracking_id=tracking_id)

    if request.user.role != "capitol_approver":
        messages.error(request, "Only Approvers can return cases for correction.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.status != "for_approval":
        messages.error(request, "This case is not eligible for return.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "Return reason is required.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.assigned_to_id is None:
        messages.error(request, "This case is not assigned to an examiner.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    old_status = case.status
    case.status = "in_review"
    case.return_reason = reason
    case.returned_at = timezone.now()
    case.returned_by = request.user
    case.save(update_fields=[
        "status",
        "return_reason",
        "returned_at",
        "returned_by",
        "updated_at",
    ])

    AuditLog.objects.create(
        actor=request.user,
        action="case_status_change",
        target_object=f"Case: {case.tracking_id}",
        details={
            "old_status": old_status,
            "new_status": case.status,
            "reason": reason,
            "returned_to": "Examiner"
        }
    )

    messages.success(request, f"Case {case.tracking_id} returned to examiner for correction.")
    return redirect("case_detail", tracking_id=case.tracking_id)


@login_required
@require_POST
def return_to_receiving(request, tracking_id):
    case = get_object_or_404(Case, tracking_id=tracking_id)

    if request.user.role != "capitol_examiner":
        messages.error(request, "Only Examiners can return cases to Receiving.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.status not in {"in_review", "for_review", "under_review"} or case.assigned_to_id != request.user.id:
        messages.error(request, "This case is not eligible for return to Receiving.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "Return reason is required.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    old_status = case.status
    case.status = "received"
    case.return_reason = reason
    case.returned_at = timezone.now()
    case.returned_by = request.user
    case.assigned_to = None
    case.assigned_at = None
    case.save(update_fields=[
        "status",
        "return_reason",
        "returned_at",
        "returned_by",
        "assigned_to",
        "assigned_at",
        "updated_at",
    ])

    AuditLog.objects.create(
        actor=request.user,
        action="case_status_change",
        target_object=f"Case: {case.tracking_id}",
        details={
            "old_status": old_status,
            "new_status": case.status,
            "reason": reason,
            "returned_to": "Receiver"
        }
    )

    messages.success(request, f"Case {case.tracking_id} returned to Receiving.")
    return redirect("case_detail", tracking_id=case.tracking_id)


@login_required
@require_POST
def mark_numbered(request, tracking_id):
    case = get_object_or_404(Case, tracking_id=tracking_id)

    if request.user.role != "capitol_numberer":
        messages.error(request, "Only Capitol Numberers can move cases to release.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.status != "for_numbering":
        messages.error(request, "This case is not eligible for numbering.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.documents.exists() and case.documents.filter(reviewed_ok=False).exists():
        messages.error(request, "Review all uploaded documents and mark them as checked before numbering.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    def parse_numbers(raw: str) -> list[str]:
        raw = (raw or "").strip()
        if not raw:
            return []
        parts = []
        for chunk in raw.replace("\n", ",").replace(" ", ",").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts.append(chunk)
        nums: list[str] = []
        for p in parts:
            if not p.isdigit():
                raise ValueError(f"Invalid number: {p}")
            if len(p) > 5:
                raise ValueError("Numbers must be at most 5 digits.")
            if int(p) <= 0:
                raise ValueError("Numbers must be positive integers.")
            nums.append(p.zfill(5))
        # de-dupe while preserving order
        seen = set()
        out = []
        for n in nums:
            if n in seen:
                continue
            seen.add(n)
            out.append(n)
        return out

    remove_raw = request.POST.getlist("remove_numbers") or []
    to_remove: set[str] = set()
    for v in remove_raw:
        s = (str(v) or "").strip()
        if s.isdigit() and len(s) <= 5 and int(s) > 0:
            to_remove.add(s.zfill(5))

    numbers_raw = (request.POST.get("numbers") or "").strip()
    try:
        to_add = parse_numbers(numbers_raw)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("case_detail", tracking_id=case.tracking_id)

    last_used = CaseNumber.objects.order_by("-number").values_list("number", flat=True).first() or ""

    with transaction.atomic():
        if to_remove:
            CaseNumber.objects.filter(case=case, number__in=sorted(to_remove)).delete()

        existing = set(CaseNumber.objects.filter(case=case).values_list("number", flat=True))
        new_numbers = [n for n in to_add if n not in existing]

        if new_numbers:
            dupe_qs = CaseNumber.objects.filter(number__in=new_numbers).exclude(case=case)
            if dupe_qs.exists():
                messages.error(request, "Duplicate number detected. Please use unique numbers.")
                return redirect("case_detail", tracking_id=case.tracking_id)

            for n in new_numbers:
                CaseNumber.objects.create(case=case, number=n, created_by=request.user)

    final_numbers = list(CaseNumber.objects.filter(case=case).values_list("number", flat=True))
    if not final_numbers:
        messages.error(request, "At least one number is required.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    old_status = case.status
    case.status = "for_release"
    case.save(update_fields=["status", "updated_at"])

    AuditLog.objects.create(
        actor=request.user,
        action="case_status_change",
        target_object=f"Case: {case.tracking_id}",
        details={"old_status": old_status, "new_status": case.status, "numbers": final_numbers}
    )

    messages.success(request, f"Case {case.tracking_id} moved to For Release.")
    return redirect("case_detail", tracking_id=case.tracking_id)


@login_required
@require_POST
def release_case(request, tracking_id):
    case = get_object_or_404(Case, tracking_id=tracking_id)

    if request.user.role != "capitol_releaser":
        messages.error(request, "Only Releasers can release cases.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.status != "for_release":
        messages.error(request, "This case is not eligible for release.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.documents.exists() and case.documents.filter(reviewed_ok=False).exists():
        messages.error(request, "Review all uploaded documents and mark them as checked before releasing.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    old_status = case.status
    case.status = "released"
    case.released_at = timezone.now()
    case.save(update_fields=["status", "released_at", "updated_at"])

    AuditLog.objects.create(
        actor=request.user,
        action="case_release",
        target_object=f"Case: {case.tracking_id}",
        details={"old_status": old_status, "new_status": case.status}
    )

    send_case_email(
        to_email=(case.client_email or "").strip(),
        subject=f"PAStrack: Case {case.tracking_id} released",
        message=(
            f"Your request {case.tracking_id} has been released.\n\n"
            f"Current status: {dict(Case.STATUS_CHOICES).get(case.status, case.status)}\n"
        ),
    )
    sns_hook(event="case_released", payload={"tracking_id": case.tracking_id, "status": case.status})

    messages.success(request, f"Case {case.tracking_id} marked as Released.")
    return redirect("case_detail", tracking_id=case.tracking_id)
