# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false

import io
import requests
import contextlib
import secrets
import string
import sys
from django.core.mail import send_mail
from datetime import timedelta, datetime
import base64
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.forms import PasswordChangeForm
from django.conf import settings
from django import forms
from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django.db import IntegrityError, models, transaction, connection
from django.db.models import Q, Count
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django.shortcuts import get_object_or_404, redirect, render
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.urls import reverse

import random
from .models import Case, CustomUser, AuditLog, LGUTaxDeclarationSequence # <--- Add it here

@login_required
def case_quick_view(request, tracking_id):
    # Fetch the case
    case = get_object_or_404(Case, tracking_id=tracking_id)
    
    # Fetch recent remarks/logs
    recent_remarks = AuditLog.objects.filter(
        target_object=f"Case: {case.tracking_id}", 
        action__icontains="remark"
    ).order_by("-created_at")[:5]

    # Check if the user is the current owner using your existing helper function
    is_owner = _user_is_current_owner_for_internal_sections(request.user, case)

    # Render just the HTML snippet
    context = {
        "case": case,
        "remarks": recent_remarks,
        "is_owner": is_owner,
    }
    html = render_to_string("core/case_detail_drawer.html", context, request=request)
    
    return HttpResponse(html)

@login_required
def lgu_submissions_view(request):
    if request.user.role != "lgu_admin":
        messages.error(request, "Not authorized.")
        return redirect("dashboard")

    tab = (request.GET.get("tab") or "").strip().lower() or "all"
    query = (request.GET.get("q") or "").strip()

    base_qs = (
        Case.objects.filter(submitted_by=request.user, lgu_submitted_at__isnull=False)
        .exclude(status="draft")
        .order_by("-lgu_submitted_at", "-created_at")
    )

    tab_map = {
        "all": None,
        "pending": {"not_received", "pending", "client_correction"},
        "processing": {"received", "to_examine", "in_review", "for_taxmapping", "for_approval", "for_numbering", "for_release"},
        "released": {"released"},
    }

    counts = {
        "all": base_qs.count(),
        "pending": base_qs.filter(status__in=tab_map["pending"]).count(),
        "processing": base_qs.filter(status__in=tab_map["processing"]).count(),
        "released": base_qs.filter(status__in=tab_map["released"]).count(),
    }

    qs = base_qs
    statuses = tab_map.get(tab)
    if statuses:
        qs = qs.filter(status__in=statuses)
    elif tab not in tab_map:
        tab = "all"

    if query:
        qs = qs.filter(
            Q(tracking_id__icontains=query)
            | Q(client_name__icontains=query)
            | Q(client_first_name__icontains=query)
            | Q(client_last_name__icontains=query)
            | Q(client_middle_name__icontains=query)
            | Q(client_suffix__icontains=query)
        )

    paginator = Paginator(qs, 15)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    context = {
        "page_obj": page_obj,
        "tab": tab,
        "query": query,
        "counts": counts,
        "tabs": [
            ("all", "All Submissions"),
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("released", "Released"),
        ],
    }
    return render(request, "lgu_submissions.html", context)


@login_required
def system_settings(request):
    user = request.user

    profile_form = SettingsProfileForm(instance=user)
    password_form = PasswordChangeForm(user=user)
    notifications_form = SettingsNotificationsForm(instance=user)
    preferences_form = SettingsPreferencesForm(instance=user)

    if request.method == "POST":
        section = (request.POST.get("section") or "").strip().lower()

        if section == "profile":
            profile_form = SettingsProfileForm(request.POST, request.FILES, instance=user)
            if profile_form.is_valid():
                profile_form.save()
                messages.success(request, "Profile updated.")
                return redirect(f"{reverse('settings')}#profile")

        elif section == "security":
            password_form = PasswordChangeForm(user=user, data=request.POST)
            if password_form.is_valid():
                now = timezone.now()
                if user.last_password_change_at and user.last_password_change_at.month != now.month:
                    user.password_change_count_this_month = 0

                if user.role != "super_admin":
                    if user.password_change_count_this_month >= 2:
                        messages.error(request, "Password change limit (twice a month) reached. Contact the Super Admin for approval.")
                        return redirect(f"{reverse('settings')}#security")

                password_form.save()
                user.must_change_password = False
                user.password_change_count_this_month += 1
                user.last_password_change_at = now
                user.save(update_fields=["must_change_password", "password_change_count_this_month", "last_password_change_at"])
                update_session_auth_hash(request, user)

                AuditLog.objects.create(
                    actor=user,
                    action="reset_password",
                    target_object=f"User: {user.email}",
                    details={"forced_reset": False, "count_this_month": user.password_change_count_this_month},
                )

                messages.success(request, "Password updated.")
                return redirect(f"{reverse('settings')}#security")

        elif section == "notifications":
            notifications_form = SettingsNotificationsForm(request.POST, instance=user)
            if notifications_form.is_valid():
                updated = notifications_form.save(commit=False)
                if user.role == "super_admin":
                    updated.notify_critical_system_alerts = True
                updated.save(update_fields=[
                    "notify_new_account_activations",
                    "notify_weekly_activity_report",
                    "notify_critical_system_alerts",
                ])
                messages.success(request, "Notification settings updated.")
                return redirect(f"{reverse('settings')}#notifications")

        elif section == "system":
            preferences_form = SettingsPreferencesForm(request.POST, instance=user)
            if preferences_form.is_valid():
                preferences_form.save()
                messages.success(request, "Preferences updated.")
                return redirect(f"{reverse('settings')}#system")
    for field in password_form.fields.values():
        field.widget.attrs.update({'class': 'form-control'})

    return render(request, "settings.html", {
        "role_display": user.get_role_display(),
        "profile_form": profile_form,
        "password_form": password_form,
        "notifications_form": notifications_form,
        "preferences_form": preferences_form,
    })
    
@login_required
def user_list_api(request):
    """API endpoint for fetching filtered users for the Super Admin dashboard tabs."""
    if request.user.role != "super_admin":
        return JsonResponse({"error": "Unauthorized"}, status=403)

    tab_id = request.GET.get("tab", "total")
    q = request.GET.get("q", "").strip()
    role = request.GET.get("role", "")
    lgu = request.GET.get("lgu", "")
    page = request.GET.get("page", 1)

    qs = CustomUser.objects.exclude(id=request.user.id).order_by("-date_joined")

    if tab_id == "pending":
        qs = qs.filter(account_status="pending")
    elif tab_id == "deactivated":
        qs = qs.filter(account_status="inactive")

    if q:
        qs = qs.filter(
            Q(full_name__icontains=q) |
            Q(email__icontains=q) |
            Q(username__icontains=q)
        )
    if role:
        qs = qs.filter(role=role)
    if lgu:
        qs = qs.filter(lgu_municipality=lgu)

    total_count = qs.count()
    paginator = Paginator(qs, 5)
    page_obj = paginator.get_page(page)

    users = []
    for user in page_obj:
        # Determine initials
        name_parts = (user.full_name or user.email).split()
        initials = "".join([p[0].upper() for p in name_parts[:2]]) if name_parts else "??"

        # Determine role class/string
        role_map = {
            "super_admin": ("role-approver", "Super Admin"),
            "lgu_admin": ("role-assessor", "LGU Admin"),
            "capitol_receiving": ("role-receiver", "Receiver"),
            "capitol_examiner": ("role-examiner", "Examiner"),
            "capitol_approver": ("role-approver", "Approver"),
            "capitol_taxmapper": ("role-examiner", "Tax Mapper"),
            "capitol_numberer": ("role-receiver", "Numberer"),
            "capitol_releaser": ("role-receiver", "Releaser"),
        }
        role_class, role_str = role_map.get(user.role, ("role-receiver", user.get_role_display()))

        # Determine status class/string
        status_map = {
            "active": ("status-active", "Active"),
            "pending": ("status-pending", "Pending"),
            "inactive": ("status-inactive", "Inactive"),
        }
        status_class, status_str = status_map.get(user.account_status, ("status-inactive", user.get_account_status_display()))

        is_capitol = user.role.startswith("capitol_") or user.role == "super_admin"
        users.append({
            "id": user.id,
            "initials": initials,
            "name": user.full_name or user.email,
            "email": user.email,
            "role_class": role_class,
            "role_str": role_str,
            "lgu": "Capitol" if is_capitol else (user.lgu_municipality or "Capitol"),
            "status_class": status_class,
            "status_str": status_str,
            "is_pending": user.account_status == "pending",
            "is_inactive": user.account_status == "inactive",
        })

    return JsonResponse({
        "users": users,
        "total_count": total_count,
        "current_page": page_obj.number,
        "has_next": page_obj.has_next(),
        "has_previous": page_obj.has_previous(),
        "num_pages": paginator.num_pages,
    })
from django.utils.html import format_html

from .forms import (
    CaseDetailsForm,
    CaseRemarkForm,
    ChecklistItemForm,
    ProfileUpdateForm,
    SettingsNotificationsForm,
    SettingsPreferencesForm,
    SettingsProfileForm,
    PublicCaseSearchForm,
    ReportFilterForm,
    StaffAccountCreateForm,
    StaffAccountUpdateForm,
    StaffSearchForm,
    SupportFeedbackForm,
)
from .models import ArchivedCaseDocument, AuditLog, Case, CaseDocument, CaseNumber, CaseRemark, CustomUser, FAQItem, SupportFeedback, DocumentVersion
from .notifications import send_case_email, sns_hook


def _municipality_area_code(name: str) -> str:
    raw = (name or "").strip()
    if not raw:
        return ""
    letters = "".join([c for c in raw.upper() if c.isalpha()])
    if len(letters) >= 3:
        return letters[:3]
    return letters


def _build_checklist_rows(formset, documents: list[CaseDocument], requirements=None):
    docs_by_key = {((d.doc_type or "").strip().lower()): d for d in (documents or [])}
    req_keys = set()
    if requirements:
        for req in requirements:
            if isinstance(req, dict):
                req_keys.add(req.get("doc_type", "").strip().lower())
            elif isinstance(req, str):
                req_keys.add(req.strip().lower())
    req_keys.add("endorsement letter")
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
            
        is_custom = selected == "__custom__" or (key and key not in req_keys)
        
        rows.append({
            "form": f,
            "doc": doc,
            "doc_type": effective_doc_type,
            "filename": filename,
            "is_custom": is_custom,
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
        "not_received": "Submitted",
        "received": "Received",
        "to_examine": "In Review",
        "for_review": "In Review",
        "in_review": "In Review",
        "for_taxmapping": "Tax Mapping",
        "for_approval": "For Approval",
        "approved": "Approved",
        "for_numbering": "For Numbering",
        "for_release": "For Release",
        "released": "Released",
        "client_correction": "Returned",
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
    add("Submitted", case.created_at)
    
    # Key transitions from audit logs
    from core.models import AuditLog
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
                add("Received", h.created_at)
                physically_received_added = True
            continue
        if action in {"case_status_change", "case_approval", "case_rejection", "case_release"}:
            details = getattr(h, "details", {}) or {}
            new_status = None
            if isinstance(details, dict):
                new_status = details.get("new_status")
            
            if new_status:
                if isinstance(details, dict) and "returned_to" in details:
                    returned_to = details["returned_to"]
                    label = f"Returned - to {returned_to}"
                    add(label, h.created_at)
                    continue

                # If the status change is 'received', handle it carefully to avoid duplicates
                if new_status == "received":
                    if not physically_received_added:
                        add("Received", h.created_at)
                        physically_received_added = True
                    continue
                
                label = _public_status_label(type("obj", (), {"status": new_status})())
                add(label, h.created_at)
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
        "case_type": case.get_case_type_display(),
        "area": case.area,
        "created_at": case.created_at,
    })


def public_track_api(request, tracking_id: str):
    """API version of track_case_detail for the React frontend."""
    tracking = (tracking_id or "").strip().upper()
    
    # Validation: Empty or too short
    if not tracking:
        return JsonResponse({"detail": "Tracking ID is required."}, status=400)
    
    case = Case.objects.filter(tracking_id__iexact=tracking, lgu_submitted_at__isnull=False).first()
    if not case:
        return JsonResponse({"detail": f"No record found for Tracking ID: {tracking}"}, status=404)

    public_status = _public_status_label(case)
    timeline_raw = _build_public_timeline(case)
    
    # Format timeline for JSON
    timeline = []
    for event in timeline_raw:
        timeline.append({
            "label": event["label"],
            "when": event["when"].isoformat() if hasattr(event["when"], "isoformat") else str(event["when"])
        })

    return JsonResponse({
        "tracking_id": case.tracking_id,
        "status": public_status,
        "updated_at": case.updated_at.isoformat(),
        "timeline": timeline,
        "client_name": f"{case.client_first_name} {case.client_last_name}" if case.client_first_name else case.client_name,
        "case_type": case.get_case_type_display(),
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
    # Security Gate: Allow Super Admins, LGU Admins, and Capitol Staff
    if request.user.role not in ['super_admin', 'lgu_admin'] and not getattr(request.user, 'role', '').startswith('capitol_'):
        messages.error(request, "Not authorized.")
        return redirect("dashboard")

    form = ReportFilterForm(request.GET or None)
    rows = []
    title = "Reports"

    if form.is_valid():
        report_type = form.cleaned_data["report_type"]
        date_from = form.cleaned_data.get("date_from")
        date_to = form.cleaned_data.get("date_to")
        status = (form.cleaned_data.get("status") or "").strip()
        sort = (form.cleaned_data.get("sort") or "-created_at").strip()

        # Contextual Data Filtering: LGU Admins only see their municipality's data
        if request.user.role == 'lgu_admin':
            mun = getattr(request.user, "lgu_municipality", "")
            qs = Case.objects.filter(submitted_by__lgu_municipality=mun)
        else:
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
    # Security Gate: Allow Super Admins, LGU Admins, and Capitol Staff
    if request.user.role not in ['super_admin', 'lgu_admin'] and not getattr(request.user, 'role', '').startswith('capitol_'):
        messages.error(request, "Not authorized.")
        return redirect("dashboard")

    form = ReportFilterForm(request.GET or None)
    if not form.is_valid():
        messages.error(request, "Invalid report parameters.")
        return redirect("reports")

    report_type = form.cleaned_data["report_type"]
    date_from = form.cleaned_data.get("date_from")
    date_to = form.cleaned_data.get("date_to")
    status = (form.cleaned_data.get("status") or "").strip()

    # Contextual Data Filtering: LGU Admins only see their municipality's data
    if request.user.role == 'lgu_admin':
        mun = getattr(request.user, "lgu_municipality", "")
        qs = Case.objects.filter(submitted_by__lgu_municipality=mun)
    else:
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


def _normalized_role(user: CustomUser) -> str:
    return (getattr(user, "role", "") or "").strip().lower()


def _is_examiner(user: CustomUser) -> bool:
    role = _normalized_role(user)
    return role in {"capitol_examiner", "examiner"} or role.endswith("_examiner")


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
        return getattr(case, "status", "") in {"to_examine", "in_review"} and getattr(case, "assigned_to_id", None) == getattr(user, "id", None)
    if role == "capitol_approver":
        return getattr(case, "status", "") == "for_approval"
    if role == "capitol_taxmapper":
        return getattr(case, "status", "") == "for_taxmapping" and getattr(case, "taxmapper_assigned_to_id", None) == getattr(user, "id", None)
    if role == "capitol_numberer":
        return getattr(case, "status", "") == "for_numbering"
    if role == "capitol_releaser":
        return getattr(case, "status", "") == "for_release"
    return False


def _case_current_holder_label(case: Case) -> str:
    status = getattr(case, "status", "") or ""
    if status == "client_correction":
        return "Returned to Client"
    if status in {"not_received", "received"}:
        return "Receiver"
    if status in {"to_examine", "in_review"}:
        return "Examiner"
    if status == "for_approval":
        return "Approver"
    if status == "for_taxmapping":
        return "Tax Mapper"
    if status == "for_numbering":
        return "Numberer"
    if status == "for_release":
        return "Releaser"
    return "System"


def _case_current_holder_detail(case: Case) -> str:
    status = getattr(case, "status", "") or ""
    if status in {"to_examine", "in_review"}:
        u = getattr(case, "assigned_to", None)
        if u:
            name = (u.get_full_name() or getattr(u, "full_name", "") or getattr(u, "email", "") or "").strip()
            return name
    if status == "for_taxmapping":
        u = getattr(case, "taxmapper_assigned_to", None)
        if u:
            name = (u.get_full_name() or getattr(u, "full_name", "") or getattr(u, "email", "") or "").strip()
            return name
    return ""

@xframe_options_sameorigin
@login_required
def download_case_document(request, doc_id: int):
    doc = get_object_or_404(CaseDocument.objects.select_related("case"), id=doc_id)
    if not _user_can_view_case(request.user, doc.case):
        raise Http404()
    if not doc.file or not (doc.file.name or "").strip():
        return HttpResponse("File is missing for this document.", status=404)

    if hasattr(doc.file, "storage") and hasattr(doc.file.storage, "exists"):
        try:
            if not doc.file.storage.exists(doc.file.name):
                return HttpResponse("File not found on the server storage.", status=404)
        except Exception:
            pass

    filename = os.path.basename(doc.file.name or "document")
    try:
        fh = doc.file.open("rb")
    except (FileNotFoundError, OSError, ValueError):
        return HttpResponse("File could not be opened.", status=404)

    response = FileResponse(fh, as_attachment=False, filename=filename)
    guessed, _ = mimetypes.guess_type(filename)
    if guessed:
        response["Content-Type"] = guessed
    else:
        if filename.lower().endswith(".pdf"):
            response["Content-Type"] = "application/pdf"
        elif filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            response["Content-Type"] = "image/*"
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


@xframe_options_sameorigin
@login_required
def download_archived_case_document(request, archive_id: int):
    a = get_object_or_404(ArchivedCaseDocument.objects.select_related("case"), id=archive_id)
    if not _user_can_view_case(request.user, a.case):
        raise Http404()
    if not a.file or not (a.file.name or "").strip():
        return HttpResponse("File is missing for this archived document.", status=404)

    if hasattr(a.file, "storage") and hasattr(a.file.storage, "exists"):
        try:
            if not a.file.storage.exists(a.file.name):
                return HttpResponse("File not found on the server storage.", status=404)
        except Exception:
            pass

    filename = os.path.basename(a.file.name or "document")
    try:
        fh = a.file.open("rb")
    except (FileNotFoundError, OSError, ValueError):
        return HttpResponse("File could not be opened.", status=404)

    response = FileResponse(fh, as_attachment=False, filename=filename)
    guessed, _ = mimetypes.guess_type(filename)
    if guessed:
        response["Content-Type"] = guessed
    else:
        if filename.lower().endswith(".pdf"):
            response["Content-Type"] = "application/pdf"
        elif filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            response["Content-Type"] = "image/*"
    response["Content-Disposition"] = f'inline; filename="{filename}"'
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

    review_remark = (request.POST.get("review_remark") or "").strip()

    if request.user.role == "capitol_receiving":
        if (doc.review_remark or "") != review_remark:
            doc.review_remark = review_remark
            doc.save(update_fields=["review_remark", "updated_at"])

        AuditLog.objects.create(
            actor=request.user,
            action="case_document_review",
            target_object=f"Case: {case.tracking_id}",
            details={"document": doc.doc_type, "checked": bool(doc.reviewed_ok), "remark": review_remark[:2000]},
        )
        messages.success(request, "Document remark saved.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    ok_raw = (request.POST.get("reviewed_ok") or "").strip().lower()
    reviewed_ok = ok_raw in {"1", "true", "yes", "y", "on"}

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
            review_remark = (request.POST.get(f"review_remark_{doc.id}") or "").strip()

            if request.user.role == "capitol_receiving":
                if (doc.review_remark or "") == review_remark:
                    continue
                doc.review_remark = review_remark
                doc.save(update_fields=["review_remark", "updated_at"])
                updates.append({"document": doc.doc_type, "checked": bool(doc.reviewed_ok), "remark": review_remark[:500]})
                continue

            reviewed_ok = (request.POST.get(f"reviewed_ok_{doc.id}") or "").strip() == "1"
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
    elif action == "case_numbered":
        tx_number = d.get("transaction_number") or d.get("td_number")
        if tx_number:
            parts.append(f"Transaction Number: {tx_number}")
        lgu = d.get("lgu")
        if lgu:
            parts.append(f"LGU: {lgu}")
        new_status = d.get("new_status")
        if new_status:
            status_label = dict(Case.STATUS_CHOICES).get(str(new_status), str(new_status))
            parts.append(f"Status: {status_label}")
    elif action == "case_document_review":
        one_doc = d.get("document")
        many_docs = d.get("documents")
        if isinstance(one_doc, str) and one_doc.strip():
            parts.append(f"Document: {one_doc.strip()}")
            if "checked" in d:
                parts.append(f"Reviewed OK: {'Yes' if bool(d.get('checked')) else 'No'}")
            remark = d.get("remark")
            if isinstance(remark, str) and remark.strip():
                parts.append(f"Remark: {remark.strip()}")
        elif isinstance(many_docs, list):
            lines = []
            for item in many_docs:
                if not isinstance(item, dict):
                    continue
                doc_name = (item.get("document") or "").strip()
                if not doc_name:
                    continue
                checked = bool(item.get("checked"))
                remark = (item.get("remark") or "").strip()
                line = f"{doc_name}: {'Reviewed OK' if checked else 'Not OK'}"
                if remark:
                    line = f"{line} — {remark}"
                lines.append(line)
            if lines:
                parts.extend(lines[:50])
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

    # ==========================================
    # COMMON CAPITOL STAFF LOGIC
    # ==========================================
    if user.role.startswith("capitol_"):
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
            "section": "capitol_staff",
            "capitol_role": user.get_role_display(),
        })

    # ==========================================
    # ROLE-SPECIFIC DASHBOARDS
    # ==========================================
    if user.role == "super_admin":
        # 1. KPIs
        total_users_count = CustomUser.objects.exclude(id=user.id).count()
        total_lgus_count = CustomUser.objects.filter(role="lgu_admin").values("lgu_municipality").distinct().count()
        active_cases_count = Case.objects.exclude(status__in=["released", "withdrawn", "returned", "draft"]).count()
        
        seven_days_ago = timezone.now() - timedelta(days=7)
        new_users_count = CustomUser.objects.exclude(id=user.id).filter(date_joined__gte=seven_days_ago).count()
        
        pending_users_count = CustomUser.objects.exclude(id=user.id).filter(account_status="pending").count()
        deactivated_users_count = CustomUser.objects.exclude(id=user.id).filter(account_status="inactive").count()

        # 2. Recent Logs
        recent_logs = AuditLog.objects.filter(
            action__in=["create_user", "deactivate_user", "reactivate_user", "update_user", "reset_password", "login", "logout", "activate_account"]
        ).select_related("actor", "target_user").order_by("-created_at")[:5]

        # 3. Chart Data (Role Distribution)
        role_map = {
            "lgu_admin": 0,
            "capitol_receiving": 1,
            "capitol_examiner": 2,
            "capitol_approver": 3,
            "capitol_numberer": 4,
            "capitol_taxmapper": 5,
            "capitol_releaser": 6,
        }
        role_distribution_array = [0] * 7
        role_counts = CustomUser.objects.values("role").annotate(count=Count("id"))
        for item in role_counts:
            idx = role_map.get(item["role"])
            if idx is not None:
                role_distribution_array[idx] = item["count"]

        # 4. Pipeline Data
        lgu_staff_count = CustomUser.objects.exclude(id=user.id).filter(role="lgu_admin").count()
        capitol_staff_count = CustomUser.objects.exclude(id=user.id).filter(role__startswith="capitol_").count()
        active_users_count = CustomUser.objects.exclude(id=user.id).filter(account_status="active").count()

        # 5. Audit Log System Activity (Timezone Aware)
        today = timezone.localdate()
        seven_days_ago_date = today - timedelta(days=6)
        
        weekly_labels = []
        weekly_data = [0] * 7
        for i in range(7):
            d = seven_days_ago_date + timedelta(days=i)
            weekly_labels.append(d.strftime("%a"))
            
        logs_7 = AuditLog.objects.filter(created_at__gte=timezone.make_aware(datetime.combine(seven_days_ago_date, datetime.min.time())))
        for log in logs_7:
            log_date = timezone.localtime(log.created_at).date()
            days_ago = (today - log_date).days
            if 0 <= days_ago <= 6:
                idx = 6 - days_ago
                weekly_data[idx] += 1
                
        weekly_max = max(weekly_data) if weekly_data else 10
        weekly_max = max(weekly_max + 10, 35)

        twenty_eight_days_ago = today - timedelta(days=27)
        monthly_labels = ["Week 1", "Week 2", "Week 3", "Week 4"]
        monthly_data = [0, 0, 0, 0]
        logs_28 = AuditLog.objects.filter(created_at__date__gte=twenty_eight_days_ago)
        for log in logs_28:
            days_ago = (today - timezone.localtime(log.created_at).date()).days
            if days_ago <= 6:
                monthly_data[3] += 1
            elif days_ago <= 13:
                monthly_data[2] += 1
            elif days_ago <= 20:
                monthly_data[1] += 1
            else:
                monthly_data[0] += 1
                
        monthly_max = max(monthly_data) if monthly_data else 50
        monthly_max = max(monthly_max + 20, 110)

        # 6. User List (Initial Load - Total Users)
        users_qs = CustomUser.objects.exclude(id=user.id).order_by("-date_joined")
        paginator = Paginator(users_qs, 5)
        page_number = request.GET.get("page", 1)
        page_obj = paginator.get_page(page_number)

        context.update({
            "section": "super_admin",
            "total_users_count": total_users_count,
            "total_lgus_count": total_lgus_count,
            "active_cases_count": active_cases_count,
            "new_users_count": new_users_count,
            "pending_users_count": pending_users_count,
            "deactivated_users_count": deactivated_users_count,
            "active_users_count": active_users_count,
            "lgu_staff_count": lgu_staff_count,
            "capitol_staff_count": capitol_staff_count,
            "recent_logs": recent_logs,
            "role_distribution_array": role_distribution_array,
            "page_obj": page_obj,
            "role_choices": CustomUser.ROLE_CHOICES,
            "lgu_choices": CustomUser.LGU_MUNICIPALITY_CHOICES,
            "sys_weekly_labels": json.dumps(weekly_labels),
            "sys_weekly_data": json.dumps(weekly_data),
            "sys_weekly_max": weekly_max,
            "sys_monthly_labels": json.dumps(monthly_labels),
            "sys_monthly_data": json.dumps(monthly_data),
            "sys_monthly_max": monthly_max,
        })
        template = "core/dashboard_superadmin.html"

    elif user.role == "lgu_admin":
        tab = (request.GET.get("tab") or "all").strip().lower()
        mun = (getattr(user, "lgu_municipality", "") or "").strip()
        
        base_qs = Case.objects.filter(lgu_submitted_at__isnull=False).select_related("submitted_by").order_by("-created_at")
        
        if mun:
            base_qs = base_qs.filter(submitted_by__lgu_municipality=mun)
        else:
            base_qs = base_qs.filter(submitted_by=user)

        base_qs = base_qs.exclude(status="client_correction", client_correction_deadline__lt=timezone.now())

        total_cases_count = base_qs.count()
        todays_cases_count = base_qs.filter(lgu_submitted_at__date=timezone.localdate()).count()

        pending_statuses = {"not_received"}
        processing_statuses = {"received", "to_examine", "in_review", "for_taxmapping", "for_approval", "approved", "for_numbering", "for_release"}
        released_statuses = {"released"}

        tab_map = {
            "all": None,
            "pending": pending_statuses,
            "processing": processing_statuses,
            "released": released_statuses,
        }
        
        statuses = tab_map.get(tab)
        qs = base_qs
        if statuses:
            qs = qs.filter(status__in=statuses)

        # Counts for tab badges
        all_count = total_cases_count
        pending_count = base_qs.filter(status__in=pending_statuses).count()
        processing_count = base_qs.filter(status__in=processing_statuses).count()
        released_count = base_qs.filter(status__in=released_statuses).count()

        paginator = Paginator(qs, 10)
        page_obj = paginator.get_page(request.GET.get("page") or 1)

        raw = list(base_qs.values("status").annotate(count=Count("id")).order_by("status"))
        status_labels = dict(Case.STATUS_CHOICES)
        status_counts = [{"status": status_labels.get(r["status"], r["status"]), "count": r["count"]} for r in raw]

        recent_logs = AuditLog.objects.filter(actor=user).order_by("-created_at")[:5]

        status_counts_dict = {r["status"]: r["count"] for r in raw}
        drafts = status_counts_dict.get("draft", 0) + status_counts_dict.get("client_correction", 0)
        not_received = status_counts_dict.get("not_received", 0)
        received = status_counts_dict.get("received", 0)
        in_review = sum(status_counts_dict.get(s, 0) for s in ["to_examine", "in_review", "for_taxmapping"])
        for_approval = status_counts_dict.get("for_approval", 0) + status_counts_dict.get("approved", 0)
        for_numbering = status_counts_dict.get("for_numbering", 0) + status_counts_dict.get("for_release", 0)
        released = status_counts_dict.get("released", 0)
        others = status_counts_dict.get("withdrawn", 0) + status_counts_dict.get("returned", 0)

        pipeline_data_dict = {
            "all": {
                "labels": ["Draft/Correction", "Not Received", "Processing", "Released", "Withdrawn/Returned"],
                "data": [drafts, not_received, (received + in_review + for_approval + for_numbering), released, others],
                "colors": ["#64748b", "#f59e0b", "#3b82f6", "#059669", "#ef4444"],
                "total": total_cases_count,
                "centerLabel": "Total Cases"
            },
            "pending": {
                "labels": ["Not Received"],
                "data": [not_received],
                "colors": ["#f59e0b"],
                "total": not_received,
                "centerLabel": "Pending"
            },
            "in_progress": {
                "labels": ["Received", "In Review", "For Approval", "For Numbering"],
                "data": [received, in_review, for_approval, for_numbering],
                "colors": ["#0ea5e9", "#6366f1", "#7c3aed", "#ec4899"],
                "total": processing_count,
                "centerLabel": "In Progress"
            },
            "completed": {
                "labels": ["Released", "Withdrawn/Returned"],
                "data": [released, others],
                "colors": ["#059669", "#ef4444"],
                "total": released_count + others,
                "centerLabel": "Completed"
            }
        }


        # Volume Chart Data
        today = timezone.localdate()
        seven_days_ago_date = today - timedelta(days=6)
        
        weekly_labels = []
        weekly_data = [0] * 7
        for i in range(7):
            d = seven_days_ago_date + timedelta(days=i)
            weekly_labels.append(d.strftime("%a"))
            
        cases_7 = base_qs.filter(lgu_submitted_at__gte=timezone.make_aware(datetime.combine(seven_days_ago_date, datetime.min.time())))
        for case in cases_7:
            if case.lgu_submitted_at:
                case_date = timezone.localtime(case.lgu_submitted_at).date()
                days_ago = (today - case_date).days
                if 0 <= days_ago <= 6:
                    idx = 6 - days_ago
                    weekly_data[idx] += 1
            
        weekly_max = max(weekly_data) if weekly_data else 5
        weekly_max = max(weekly_max + 5, 25)

        twenty_eight_days_ago = today - timedelta(days=27)
        monthly_labels = ["Week 1", "Week 2", "Week 3", "Week 4"]
        monthly_data = [0, 0, 0, 0]
        cases_28 = base_qs.filter(lgu_submitted_at__gte=timezone.make_aware(datetime.combine(twenty_eight_days_ago, datetime.min.time())))
        for case in cases_28:
            if case.lgu_submitted_at:
                days_ago = (today - timezone.localtime(case.lgu_submitted_at).date()).days
                if days_ago <= 6:
                    monthly_data[3] += 1
                elif days_ago <= 13:
                    monthly_data[2] += 1
                elif days_ago <= 20:
                    monthly_data[1] += 1
                elif days_ago <= 27:
                    monthly_data[0] += 1
        
        monthly_max = max(monthly_data) if monthly_data else 20
        monthly_max = max(monthly_max + 10, 80)

        volume_chart_data = {
            "weekly": {"labels": weekly_labels, "data": weekly_data, "max": weekly_max},
            "monthly": {"labels": monthly_labels, "data": monthly_data, "max": monthly_max}
        }
        
        context.update({
            "section": "lgu_admin",
            "tab": tab,
            "tabs": [
                ("all", "All Submissions", all_count), 
                ("pending", "Pending", pending_count), 
                ("processing", "Processing", processing_count),
                ("released", "Released", released_count)
            ],
            "page_obj": page_obj,
            "status_counts": status_counts,
            "total_cases_count": total_cases_count,
            "todays_cases_count": todays_cases_count,
            "recent_logs": recent_logs,
            "pending_count": pending_count,
            "processing_count": processing_count,
            "released_count": released_count,
            "status_not_received": not_received,
            "status_received": received,
            "status_in_review": in_review,
            "status_for_approval": for_approval,
            "status_for_numbering": for_numbering,
            "status_released": released,
            "lgu_volume_data_json": json.dumps(volume_chart_data),
            "pipeline_data_json": json.dumps(pipeline_data_dict),
        })
        template = "core/dashboard_lgu.html"

    elif user.role == "capitol_receiving":
        tab = (request.GET.get("tab") or "").strip().lower() or "pending"
        q = (request.GET.get("q") or "").strip()

        # Base Queries
        base_pending_qs = Case.objects.filter(status="not_received")
        base_received_qs = Case.objects.filter(status="received", assigned_to__isnull=True)

        if q:
            filt = Q(tracking_id__icontains=q) | Q(client_name__icontains=q) | Q(submitted_by__lgu_municipality__icontains=q)
            base_pending_qs = base_pending_qs.filter(filt)
            base_received_qs = base_received_qs.filter(filt)
        
        pending_count = base_pending_qs.count()
        received_count = base_received_qs.count()

        if tab == "received":
            active_qs = base_received_qs.select_related("submitted_by").order_by("-received_at")
        else:
            tab = "pending"
            active_qs = base_pending_qs.select_related("submitted_by").order_by("-created_at")
            
        paginator = Paginator(active_qs, 10)
        page_obj = paginator.get_page(request.GET.get("page") or 1)

        # KPIs
        today = timezone.localdate()
        stats_pending_intake = Case.objects.filter(status="not_received").count()
        stats_ready_assign = Case.objects.filter(status="received", assigned_to__isnull=True).count()
        stats_received_today = AuditLog.objects.filter(actor=user, action="case_receipt", created_at__date=today).count()
        stats_returned_from_examiner = Case.objects.filter(status="received", assigned_to__isnull=True, returned_by__role="capitol_examiner").count()

        # Intake Volume Chart Data
        seven_days_ago = today - timedelta(days=6)
        weekly_labels = []
        weekly_data = [0]*7
        for i in range(7):
            d = seven_days_ago + timedelta(days=i)
            weekly_labels.append(d.strftime("%a"))
            
        rec_cases_7 = Case.objects.filter(lgu_submitted_at__gte=timezone.make_aware(datetime.combine(seven_days_ago, datetime.min.time())))
        for case in rec_cases_7:
            if case.lgu_submitted_at:
                case_date = timezone.localtime(case.lgu_submitted_at).date()
                days_ago = (today - case_date).days
                if 0 <= days_ago <= 6:
                    idx = 6 - days_ago
                    weekly_data[idx] += 1
                    
        weekly_max = max(weekly_data + [10])
        
        # Monthly Intake
        twenty_eight_days_ago = today - timedelta(days=27)
        monthly_labels = ['Week 1','Week 2','Week 3','Week 4']
        monthly_data = [0]*4
        rec_cases_28 = Case.objects.filter(lgu_submitted_at__gte=timezone.make_aware(datetime.combine(twenty_eight_days_ago, datetime.min.time())))
        for case in rec_cases_28:
            if case.lgu_submitted_at:
                days_ago = (today - timezone.localtime(case.lgu_submitted_at).date()).days
                if days_ago <= 6:
                    monthly_data[3] += 1
                elif days_ago <= 13:
                    monthly_data[2] += 1
                elif days_ago <= 20:
                    monthly_data[1] += 1
                elif days_ago <= 27:
                    monthly_data[0] += 1
        monthly_max = max(monthly_data + [100])
        
        volume_chart_data = {
            "weekly": {"labels": weekly_labels, "data": weekly_data, "max": weekly_max},
            "monthly": {"labels": monthly_labels, "data": monthly_data, "max": monthly_max}
        }
        
        # Pipeline Workload Chart Data
        all_active = Case.objects.exclude(status__in=["released", "withdrawn", "returned", "draft", "client_correction"])
        
        status_counts = {
            "received": all_active.filter(status="received").count(),
            "to_examine": all_active.filter(status__in=["to_examine", "in_review", "for_taxmapping"]).count(),
            "to_approve": all_active.filter(status__in=["for_approval", "approved"]).count(),
            "for_numbering": all_active.filter(status="for_numbering").count(),
            "to_release": all_active.filter(status="for_release").count(),
        }
        total_active = sum(status_counts.values())
        
        pipeline_all = {
            "labels": ['Received','To Examine','To Approve','For Numbering','To Release'],
            "data": [
                status_counts["received"],
                status_counts["to_examine"],
                status_counts["to_approve"],
                status_counts["for_numbering"],
                status_counts["to_release"]
            ],
            "colors": ['#059669','#7c3aed','#3b82f6','#d97706','#0ea5e9'],
            "total": total_active,
            "center_label": "Total Active"
        }
        
        pipeline_received = {
            "labels": ['Unassigned / Pending Check'],
            "data": [status_counts["received"]],
            "colors": ['#059669'],
            "total": status_counts["received"],
            "center_label": "Received"
        }
        
        examiners = CustomUser.objects.filter(role="capitol_examiner", is_active=True).annotate(
            active_load=Count("assigned_cases", filter=Q(assigned_cases__status__in=["to_examine", "in_review"]))
        ).order_by("active_load", "full_name", "email")
        
        examiner_labels = [ex.full_name or ex.email for ex in examiners]
        examiner_data = [ex.active_load for ex in examiners]
        examiner_colors = ['#8b5cf6','#a78bfa','#c4b5fd','#ddd6fe','#ede9fe'][:len(examiner_labels)]
        
        pipeline_to_examine = {
            "labels": examiner_labels,
            "data": examiner_data,
            "colors": examiner_colors,
            "total": status_counts["to_examine"],
            "center_label": "To Examine"
        }
        
        pipeline_to_approve = {
            "labels": ['Pending Approval'],
            "data": [status_counts["to_approve"]],
            "colors": ['#3b82f6'],
            "total": status_counts["to_approve"],
            "center_label": "To Approve"
        }
        
        pipeline_for_numbering = {
            "labels": ['System Queue','Manual Hold'],
            "data": [status_counts["for_numbering"], 0],
            "colors": ['#f59e0b','#fcd34d'],
            "total": status_counts["for_numbering"],
            "center_label": "For Numbering"
        }
        
        pipeline_to_release = {
            "labels": ['Counter 1','Counter 2'],
            "data": [status_counts["to_release"]//2 + (status_counts["to_release"]%2 if i==0 else 0) for i in range(2)],
            "colors": ['#0ea5e9','#7dd3fc'],
            "total": status_counts["to_release"],
            "center_label": "To Release"
        }
        
        pipeline_chart_data = {
            "all": pipeline_all,
            "received": pipeline_received,
            "to_examine": pipeline_to_examine,
            "to_approve": pipeline_to_approve,
            "for_numbering": pipeline_for_numbering,
            "to_release": pipeline_to_release,
        }
        
        # Secondary Panels
        returned_cases = Case.objects.filter(
            status="received", 
            assigned_to__isnull=True, 
            returned_by__isnull=False 
        ).select_related("returned_by").order_by("-returned_at")[:5]

        cases_to_assign = Case.objects.filter(
            status="received", 
            assigned_to__isnull=True, 
            returned_by__isnull=True 
        ).select_related("submitted_by").order_by("updated_at")[:3]

        recent_logs = AuditLog.objects.filter(actor=user).order_by("-created_at")[:6]

        context.update({
            "section": "capitol_receiving",
            "tab": tab,
            "tabs": [("pending", "Pending Intake", pending_count), ("received", "Received (Unassigned)", received_count)],
            "page_obj": page_obj,
            "stats_pending_intake": stats_pending_intake,
            "stats_ready_assign": stats_ready_assign,
            "stats_received_today": stats_received_today,
            "stats_returned_from_examiner": stats_returned_from_examiner,
            "volume_chart_data_json": json.dumps(volume_chart_data),
            "pipeline_chart_data_json": json.dumps(pipeline_chart_data),
            "returned_cases": returned_cases,
            "cases_to_assign": cases_to_assign,
            "recent_logs": recent_logs,
            "examiners": examiners,
            "q": q,
        })
        template = "core/dashboard_receiver.html"

    elif user.role == "capitol_examiner":
        today = timezone.localdate()
        
        q = (request.GET.get("q") or "").strip()
        case_type_filter = (request.GET.get("case_type") or "").strip()
        lgu_filter = (request.GET.get("lgu") or "").strip()
        
        base_qs = Case.objects.filter(assigned_to=user).select_related("submitted_by")
        
        # KPIs
        stats_assigned_today = base_qs.filter(assigned_at__date=today).count()
        stats_pending_intake = base_qs.filter(status="to_examine").count()
        stats_under_review = base_qs.filter(status="in_review").count()
        stats_returned = base_qs.filter(status="in_review", returned_by__role="capitol_approver").count()
        stats_total_handled = AuditLog.objects.filter(actor=user, action="case_status_change", details__new_status="for_approval").count()

        # Volume Chart Data
        week_start = today - timedelta(days=today.weekday())
        weekly_labels = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']
        weekly_data = [0]*7
        for i in range(7):
            day = week_start + timedelta(days=i)
            weekly_data[i] = AuditLog.objects.filter(actor=user, action="case_status_change", details__new_status="for_approval", created_at__date=day).count()
        weekly_max = max(weekly_data + [25])
        
        month_start = today.replace(day=1)
        monthly_labels = ['Week 1','Week 2','Week 3','Week 4']
        monthly_data = [0]*4
        for week_num in range(4):
            week_start_date = month_start + timedelta(weeks=week_num)
            week_end_date = week_start_date + timedelta(days=6)
            monthly_data[week_num] = AuditLog.objects.filter(actor=user, action="case_status_change", details__new_status="for_approval", created_at__date__gte=week_start_date, created_at__date__lte=week_end_date).count()
        monthly_max = max(monthly_data + [100])
        
        volume_chart_data = {
            "weekly": {"labels": weekly_labels, "data": weekly_data, "max": weekly_max},
            "monthly": {"labels": monthly_labels, "data": monthly_data, "max": monthly_max}
        }

        # Main Table Queue
        active_qs = base_qs.filter(status__in=["to_examine", "in_review", "client_correction"]).order_by("-assigned_at")
        
        # Workload Chart Data
        total_active_all = active_qs.count()
        workload_all = {
            "labels": ['Pending Review', 'Under Review', 'Returned to LGU', 'Completed'],
            "data": [
                stats_pending_intake,
                stats_under_review,
                base_qs.filter(status="client_correction").count(),
                stats_total_handled
            ],
            "colors": ['#f59e0b', '#6366f1', '#ef4444', '#22c55e'],
            "total": total_active_all + stats_total_handled,
            "centerLabel": 'All Cases'
        }
        
        type_counts = list(active_qs.values('case_type').annotate(count=Count('id')).order_by('-count'))
        workload_type = {
            "labels": [dict(Case.CASE_TYPE_CHOICES).get(t['case_type'], t['case_type']) for t in type_counts] or ["No Data"],
            "data": [t['count'] for t in type_counts] or [1],
            "colors": ['#3b82f6', '#8b5cf6', '#f59e0b', '#10b981', '#ef4444', '#06b6d4'][:max(len(type_counts), 1)],
            "total": total_active_all,
            "centerLabel": 'By Type'
        }
        
        lgu_counts = list(active_qs.values('submitted_by__lgu_municipality').annotate(count=Count('id')).order_by('-count')[:5])
        workload_lgu = {
            "labels": [t['submitted_by__lgu_municipality'] or "Unknown" for t in lgu_counts] or ["No Data"],
            "data": [t['count'] for t in lgu_counts] or [1],
            "colors": ['#0ea5e9', '#8b5cf6', '#f97316', '#94a3b8', '#10b981'][:max(len(lgu_counts), 1)],
            "total": total_active_all,
            "centerLabel": 'By LGU'
        }
        
        workload_chart_data = {
            "all": workload_all,
            "by_type": workload_type,
            "by_lgu": workload_lgu
        }
        
        if q:
            active_qs = active_qs.filter(
                Q(tracking_id__icontains=q) | 
                Q(client_name__icontains=q) |
                Q(client_first_name__icontains=q) |
                Q(client_last_name__icontains=q)
            )
        if case_type_filter:
            active_qs = active_qs.filter(case_type=case_type_filter)
        if lgu_filter:
            active_qs = active_qs.filter(submitted_by__lgu_municipality=lgu_filter)

        paginator = Paginator(active_qs, 5)
        page_obj = paginator.get_page(request.GET.get("page") or 1)

        under_review_cases = base_qs.filter(status="in_review").order_by("assigned_at")[:4]
        recent_logs = AuditLog.objects.filter(actor=user).order_by("-created_at")[:4]

        context.update({
            "section": "capitol_examiner",
            "stats_assigned_today": stats_assigned_today,
            "stats_pending_intake": stats_pending_intake,
            "stats_under_review": stats_under_review,
            "stats_returned": stats_returned,
            "stats_total_handled": stats_total_handled,
            "page_obj": page_obj,
            "under_review_cases": under_review_cases,
            "recent_logs": recent_logs,
            "volume_chart_data": volume_chart_data,
            "workload_chart_data": workload_chart_data,
            "filter_q": q,
            "filter_case_type": case_type_filter,
            "filter_lgu": lgu_filter,
            "lgu_choices": CustomUser.LGU_MUNICIPALITY_CHOICES,
            "case_type_choices": Case.CASE_TYPE_CHOICES,
        })
        template = "core/dashboard_examiner.html"

    elif user.role == "capitol_approver":
        today = timezone.localdate()

        stats_pending_approval = Case.objects.filter(status="for_approval").count()
        stats_approved_today = AuditLog.objects.filter(actor=user, action="case_approval", created_at__date=today).count()
        stats_total_signed = AuditLog.objects.filter(actor=user, action="case_approval").count()
        stats_cases_denied = AuditLog.objects.filter(actor=user, action="case_status_change", details__new_status="in_review").count()

        qs = Case.objects.filter(status="for_approval").select_related("assigned_to", "submitted_by").order_by("updated_at")
        paginator = Paginator(qs, 10)
        page_obj = paginator.get_page(request.GET.get("page") or 1)

        approved_logs = AuditLog.objects.filter(actor=user, action="case_approval", created_at__date=today).order_by("-created_at")[:5]
        approved_tracking_ids = [log.target_object.replace("Case: ", "") for log in approved_logs]
        
        approved_today_cases = []
        if approved_tracking_ids:
            cases_dict = {c.tracking_id: c for c in Case.objects.filter(tracking_id__in=approved_tracking_ids)}
            approved_today_cases = [cases_dict[tid] for tid in approved_tracking_ids if tid in cases_dict]

        staff_activity = AuditLog.objects.filter(
            action__in=["case_status_change", "case_receipt", "case_assignment"]
        ).exclude(actor=user).order_by("-created_at")[:6]

        context.update({
            "section": "capitol_approver",
            "stats_pending_approval": stats_pending_approval,
            "stats_approved_today": stats_approved_today,
            "stats_total_signed": stats_total_signed,
            "stats_cases_denied": stats_cases_denied,
            "page_obj": page_obj,
            "approved_today_cases": approved_today_cases,
            "staff_activity": staff_activity,
        })
        template = "core/dashboard_approver.html"

    elif user.role == "capitol_taxmapper":
        today = timezone.localdate()

        stats_pending_mapping = Case.objects.filter(status="for_taxmapping", taxmapper_assigned_to=user).count()
        stats_completed_today = AuditLog.objects.filter(actor=user, action="case_status_change", details__taxmapped=True, created_at__date=today).count()
        stats_total_mapped = AuditLog.objects.filter(actor=user, action="case_status_change", details__taxmapped=True).count()

        qs = Case.objects.filter(status="for_taxmapping", taxmapper_assigned_to=user).select_related("submitted_by").order_by("updated_at")
        paginator = Paginator(qs, 10)
        page_obj = paginator.get_page(request.GET.get("page") or 1)

        mapped_logs = AuditLog.objects.filter(actor=user, action="case_status_change", details__taxmapped=True).order_by("-created_at")[:5]
        mapped_tracking_ids = [log.target_object.replace("Case: ", "") for log in mapped_logs]
        
        recently_mapped_cases = []
        if mapped_tracking_ids:
            cases_dict = {c.tracking_id: c for c in Case.objects.filter(tracking_id__in=mapped_tracking_ids)}
            recently_mapped_cases = [cases_dict[tid] for tid in mapped_tracking_ids if tid in cases_dict]

        routing_activity = AuditLog.objects.filter(
            action="case_status_change", 
            details__new_status="for_taxmapping"
        ).exclude(actor=user).order_by("-created_at")[:6]

        context.update({
            "section": "capitol_taxmapper",
            "stats_pending_mapping": stats_pending_mapping,
            "stats_completed_today": stats_completed_today,
            "stats_total_mapped": stats_total_mapped,
            "page_obj": page_obj,
            "recently_mapped_cases": recently_mapped_cases,
            "routing_activity": routing_activity,
        })
        template = "core/dashboard_taxmapper.html"

    elif user.role == "capitol_numberer":
        today = timezone.localdate()

        tab = (request.GET.get("tab") or "not_numbered").strip().lower()

        qs_pending = Case.objects.filter(status="for_numbering").select_related("assigned_to", "submitted_by").order_by("updated_at")
        qs_numbered = (
            Case.objects.exclude(td_number__isnull=True)
            .exclude(td_number="")
            .select_related("assigned_to", "submitted_by")
            .order_by("-updated_at")
        )

        if tab == "numbered":
            paginator = Paginator(qs_numbered, 10)
            page_obj = paginator.get_page(request.GET.get("page") or 1)
        else:
            tab = "not_numbered"
            paginator = Paginator(qs_pending, 10)
            page_obj = paginator.get_page(request.GET.get("page") or 1)

        stats_pending = qs_pending.count()
        stats_numbered_total = qs_numbered.count()
        stats_numbered_today = AuditLog.objects.filter(actor=user, action="case_numbered", created_at__date=today).count()

        sequences = list(LGUTaxDeclarationSequence.objects.all())
        featured_sequence = random.choice(sequences) if sequences else None

        recent_activity = AuditLog.objects.filter(
            actor=user,
            action__in=["login", "logout", "case_numbered"]
        ).order_by("-created_at")[:3]

        context.update({
            "section": "capitol_numberer",
            "page_obj": page_obj,
            "recent_activity": recent_activity,
            "stats_pending": stats_pending,
            "stats_numbered_today": stats_numbered_today,
            "featured_sequence": featured_sequence,
            "tab": tab,
            "stats_numbered_total": stats_numbered_total,
        })
        template = "core/dashboard_numberer.html"

    elif user.role == "capitol_releaser":
        today = timezone.localdate()
        start_of_week = today - timedelta(days=today.weekday())

        qs = Case.objects.filter(status="for_release").select_related("assigned_to", "submitted_by").order_by("updated_at")
        paginator = Paginator(qs, 10)
        page_obj = paginator.get_page(request.GET.get("page") or 1)

        stats_pending = qs.count()
        stats_released_today = AuditLog.objects.filter(actor=user, action="case_released", created_at__date=today).count()
        stats_released_week = AuditLog.objects.filter(actor=user, action="case_released", created_at__date__gte=start_of_week).count()
        stats_total_released = AuditLog.objects.filter(actor=user, action="case_released").count()

        recent_activity = AuditLog.objects.filter(
            actor=user,
            action__in=["login", "logout", "case_released"]
        ).order_by("-created_at")[:4]

        context.update({
            "section": "capitol_releaser",
            "page_obj": page_obj,
            "stats_pending": stats_pending,
            "stats_released_today": stats_released_today,
            "stats_released_week": stats_released_week,
            "stats_total_released": stats_total_released,
            "recent_activity": recent_activity,
        })
        template = "core/dashboard_releaser.html"
        
    else:
        template = "core/dashboard_capitol.html" # Fallback if no matching role

    return render(request, template, context)

@login_required
def assign_td_number(request, tracking_id):
    if request.user.role != "capitol_numberer" and request.user.role != "super_admin":
        messages.error(request, "Unauthorized.")
        return redirect("dashboard")

    case = get_object_or_404(Case, tracking_id=tracking_id, status="for_numbering")

    if request.method != "POST":
        return redirect("dashboard")

    messages.error(request, "Automatic numbering has been removed. Open the transaction and enter the Transaction Number manually.")
    return redirect("case_detail", tracking_id=case.tracking_id)




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
def export_audit_logs_csv(request):
    # Security Gate: Allow Super Admins and LGU Admins
    if request.user.role not in ['super_admin', 'lgu_admin']:
        messages.error(request, "Not authorized.")
        return redirect("dashboard")

    # Filter logs so LGU Admins only export their own actions
    if request.user.role == 'lgu_admin':
        qs = AuditLog.objects.filter(actor=request.user).select_related("actor", "target_user")
    else:
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
def delete_user(request, user_id):
    denial = _require_super_admin(request)
    if denial:
        return denial

    target = get_object_or_404(CustomUser, id=user_id)
    if target.id == request.user.id:
        messages.error(request, "You cannot delete your own account.")
        return redirect("user_management")

    AuditLog.objects.create(
        actor=request.user,
        action="delete_user",
        target_object=f"User Deleted: {target.email} ({target.username})",
        details={"full_name": target.full_name, "role": target.role}
    )
    
    target.delete()
    messages.success(request, "User account has been permanently deleted.")
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
        form = ProfileUpdateForm(request.POST, request.FILES, instance=request.user, user=request.user)
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
    role = getattr(user, "role", "") or ""
    if role == "capitol_receiving":
        # Receivers "own" the case if it is in the intake or correction phase
        return case.status in {"draft", "not_received", "client_correction"} and case.assigned_to_id is None
    if role == "capitol_examiner":
        return case.assigned_to_id == user.id
    if role != "lgu_admin":
        return False
    user_mun = (getattr(user, "lgu_municipality", "") or "").strip()
    case_mun = (getattr(getattr(case, "submitted_by", None), "lgu_municipality", "") or "").strip()
    return bool(user_mun and case_mun and user_mun == case_mun)

def _lgu_can_edit_details(user, case: Case) -> bool:
    if not _lgu_owns_case(user, case):
        return False
    
    role = getattr(user, "role", "") or ""
    
    # Receiver can edit if in intake or correction phase
    if role == "capitol_receiving":
        return case.status in {"draft", "not_received", "client_correction"}
        
    if role == "capitol_examiner":
        returned_by_role = getattr(getattr(case, "returned_by", None), "role", "") or ""
        return case.status == "in_review" and returned_by_role == "capitol_approver"
        
    # LGU can only edit if NOT yet submitted to capitol
    if role == "lgu_admin":
        if case.lgu_submitted_at is not None:
            return False
        return case.status in {"draft", "not_received", "returned"}
        
    return False

def _lgu_can_edit_documents(user, case: Case) -> bool:
    if not _lgu_can_edit_details(user, case):
        return False
    
    role = getattr(user, "role", "") or ""
    if role == "capitol_receiving":
        return True
        
    if role == "capitol_examiner":
        return True
        
    if role == "lgu_admin":
        if case.lgu_submitted_at is not None:
            return False
        return case.status in {"draft", "not_received", "returned"}
        
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


def _maybe_convert_office_upload_to_pdf(uploaded_file):
    """
    Converts .doc/.docx to .pdf using ConvertAPI REST endpoint.
    Bypasses SDK path-handling bugs by using direct HTTP POST with memory streams.
    Extracts Base64 file data directly from the response for faster processing.
    """
    filename = getattr(uploaded_file, "name", "").lower()
    
    # If it's not a Word document, return it untouched
    if not filename.endswith((".doc", ".docx")):
        return uploaded_file, {"converted": False}

    api_secret = os.getenv("CONVERTAPI_SECRET")
    if not api_secret:
        print("[ConvertAPI Debug] Error: CONVERTAPI_SECRET not set in environment variables.")
        return uploaded_file, {"converted": False}
    else:
        # Mask the secret for logging
        masked_secret = api_secret[:4] + "*" * (len(api_secret) - 8) + api_secret[-4:] if len(api_secret) > 8 else "****"
        print(f"[ConvertAPI Debug] API Secret found: {masked_secret}")

    try:
        print(f"[ConvertAPI Debug] Starting conversion for: {filename}")
        
        # 1. Prepare the format strings
        ext = os.path.splitext(filename)[1].lower()
        from_fmt = ext.replace('.', '')
        
        # 2. Read file into memory to avoid any pointer/locking issues on Windows
        uploaded_file.seek(0)
        file_content = uploaded_file.read()
        print(f"[ConvertAPI Debug] Input file read into memory: {len(file_content)} bytes")
        
        # 3. Call ConvertAPI REST endpoint directly
        url = f"https://v2.convertapi.com/convert/{from_fmt}/to/pdf?Secret={api_secret}"
        print(f"[ConvertAPI Debug] URL: {url}")
        
        # Using the file content directly
        files = {
            'File': (filename, file_content)
        }
        
        response = requests.post(url, files=files, timeout=60)
        print(f"[ConvertAPI Debug] Response Status: {response.status_code}")
        
        if response.status_code != 200:
            print(f"[ConvertAPI Debug] Response Error Body: {response.text}")
            
        response.raise_for_status()
        
        data = response.json()
        
        # 4. Safely extract the Base64 data from the nested 'Files' array
        if 'Files' not in data or not data['Files']:
            print("[ConvertAPI Debug] Error: No 'Files' in response JSON")
            raise Exception("No converted files returned from ConvertAPI")
            
        file_info = data['Files'][0]
        file_data_b64 = file_info.get('FileData')
        
        if not file_data_b64:
            raise Exception("ConvertAPI returned a file object without 'FileData'.")
            
        # Decode the Base64 string back into raw PDF bytes
        pdf_content = base64.b64decode(file_data_b64)
        print(f"[ConvertAPI Debug] Decoded PDF Size: {len(pdf_content)} bytes")
        
        # 5. Prepare the final ContentFile
        base_name = os.path.splitext(getattr(uploaded_file, "name", "document"))[0]
        if not base_name:
            base_name = "document"
        new_filename = base_name + ".pdf"
        
        final_file = ContentFile(pdf_content, name=new_filename)
        print(f"[ConvertAPI Debug] Successfully converted to: {new_filename}")
        
        # Reset the original file pointer just in case
        uploaded_file.seek(0)
        return final_file, {"converted": True}

    except Exception as e:
        print(f"[ConvertAPI Error] Failed to convert document via REST: {e}", file=sys.stderr)
        
        # Ensure pointer is reset before returning original
        try:
            uploaded_file.seek(0)
        except Exception:
            pass
            
        # Fallback to saving original document
        return uploaded_file, {"converted": False}

def _purge_expired_archived_case_documents(*, case: Case | None = None) -> None:
    now = timezone.now()
    qs = ArchivedCaseDocument.objects.filter(keep_until__lt=now)
    if case is not None:
        qs = qs.filter(case=case)
    for a in list(qs.only("id", "file")):
        if getattr(a, "file", None):
            with contextlib.suppress(Exception):
                a.file.delete(save=False)
        with contextlib.suppress(Exception):
            a.delete()


def _purge_all_archived_case_documents(*, case: Case) -> None:
    qs = ArchivedCaseDocument.objects.filter(case=case)
    for a in list(qs.only("id", "file")):
        if getattr(a, "file", None):
            with contextlib.suppress(Exception):
                a.file.delete(save=False)
        with contextlib.suppress(Exception):
            a.delete()


def _archive_existing_case_document_for_one_week(*, case: Case, doc: CaseDocument, actor: CustomUser | None) -> None:
    if getattr(case, "status", "") != "client_correction":
        return
    if not getattr(doc, "file", None):
        return

    with contextlib.suppress(Exception):
        _purge_expired_archived_case_documents(case=case)

    previous_name = os.path.basename(doc.file.name or "")
    try:
        doc.file.open("rb")
        content = doc.file.read()
    finally:
        with contextlib.suppress(Exception):
            doc.file.close()

    keep_until = timezone.now() + timedelta(days=7)
    ArchivedCaseDocument.objects.create(
        case=case,
        doc_type=(getattr(doc, "doc_type", "") or ""),
        file=ContentFile(content, name=(previous_name or f"{(getattr(doc, 'doc_type', '') or 'document')}.pdf")),
        original_filename=previous_name,
        archived_by=actor,
        keep_until=keep_until,
    )


def _upsert_case_document(*, case: Case, doc_type: str, uploaded_file, actor: CustomUser | None):
    doc_type = (doc_type or "").strip()
    if not doc_type or not uploaded_file:
        return None

    doc, created = CaseDocument.objects.get_or_create(
        case=case,
        doc_type=doc_type,
        defaults={"uploaded_by": actor},
    )
    previous_name = ""
    if not created and doc.file:
        previous_name = os.path.basename(doc.file.name or "")
        with contextlib.suppress(Exception):
            _archive_existing_case_document_for_one_week(case=case, doc=doc, actor=actor)
        with contextlib.suppress(Exception):
            doc.file.delete(save=False)

    final_file, convert_info = _maybe_convert_office_upload_to_pdf(uploaded_file)
    final_name = os.path.basename(getattr(final_file, "name", "") or "") or os.path.basename(getattr(uploaded_file, "name", "") or "")

    doc.file = final_file
    doc.uploaded_by = actor
    doc.save(update_fields=["file", "uploaded_by", "updated_at"])
    return {
        "document": doc,
        "created": bool(created),
        "previous_filename": previous_name,
        "filename": final_name,
        "converted_to_pdf": bool(convert_info.get("converted")),
    }


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
        form = CaseDetailsForm(request.POST, request.FILES, user=request.user)
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

                AuditLog.objects.create(
                    actor=request.user,
                    action="case_update",
                    target_object=f"Draft: {case.draft_id}",
                    details={"step": 1, "note": "Draft updated."}
                )

                if wants_save_draft and not wants_continue:
                    messages.success(request, "Draft saved.")
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

            AuditLog.objects.create(
                actor=request.user,
                action="case_create",
                target_object=f"Draft: {case.draft_id}",
                details={"step": 1, "note": "Draft initialized."}
            )

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

            # Lead to 2nd stage instead of Draft dashboard
            if "save_draft" in request.POST:
                messages.success(request, "Draft created.")
                return redirect("drafts")
            return redirect("draft_wizard", draft_id=case.draft_id, step=2)
    else:
        form = CaseDetailsForm(user=request.user)

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

    if request.user.role not in {"lgu_admin", "capitol_receiving", "capitol_examiner"}:
        messages.error(request, "Only LGU Admins, Receivers, and Examiners can edit submissions.")
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
            form = CaseDetailsForm(request.POST, request.FILES, instance=case, user=request.user)
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
                    if updated.lgu_submitted_at is None or updated.status in {"returned", "client_correction", "draft"}:
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
            form = CaseDetailsForm(instance=case, user=request.user)

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
                    "rows": _build_checklist_rows(formset, docs, requirements=requirements),
                    "case_type_requirements": requirements,
                })

            formset = FormSet(request.POST, request.FILES, form_kwargs={"doc_type_choices": doc_type_choices})
            if formset.is_valid():
                new_checklist = []
                seen = set()
                upload_changes: list[dict[str, object]] = []

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
                            "rows": _build_checklist_rows(formset, docs, requirements=requirements),
                            "case_type_requirements": requirements,
                        })
                    seen.add(key)

                    uploaded_file = cd.get("file")
                    is_deleted = cd.get("is_deleted")
                    if is_deleted:
                        # User explicitly cleared/deleted the file for this doc_type
                        to_del = CaseDocument.objects.filter(case=case, doc_type=doc_type)
                        for d in to_del:
                            if d.file:
                                with contextlib.suppress(Exception):
                                    d.file.delete(save=False)
                            d.delete()
                    elif uploaded_file:
                        try:
                            change = _upsert_case_document(case=case, doc_type=doc_type, uploaded_file=uploaded_file, actor=request.user)
                        except ValueError as exc:
                            messages.error(request, str(exc))
                            docs = list(case.documents.all())
                            return render(request, "core/submit_case.html", {
                                "step": 2,
                                "formset": formset,
                                "case": case,
                                "is_edit": True,
                                "documents": docs,
                                "documents_by_type": {d.doc_type: d for d in docs},
                                "rows": _build_checklist_rows(formset, docs, requirements=requirements),
                                "case_type_requirements": requirements,
                            })
                        if isinstance(change, dict):
                            upload_changes.append({
                                "doc_type": doc_type,
                                "filename": change.get("filename") or "",
                                "previous_filename": change.get("previous_filename") or "",
                                "converted_to_pdf": bool(change.get("converted_to_pdf")),
                            })

                    has_doc = CaseDocument.objects.filter(case=case, doc_type=doc_type).exists()
                    
                    is_custom = cd.get("doc_type") == "__custom__" or doc_type not in requirements
                    if is_custom and doc_type != "Endorsement Letter" and (is_deleted or not has_doc):
                        continue
                        
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
                update_fields = ["checklist", "updated_at"]
                
                # Cleanup: remove CaseDocument files that are no longer in the checklist
                current_doc_types = {item["doc_type"] for item in new_checklist}
                to_delete = CaseDocument.objects.filter(case=case).exclude(doc_type__in=current_doc_types)
                for d in to_delete:
                    if d.file:
                        with contextlib.suppress(Exception):
                            d.file.delete(save=False)
                    d.delete()

                if case.status == "returned":
                    case.status = "not_received"
                    case.client_correction_deadline = None
                    case.lgu_submitted_at = None
                    update_fields.extend(["status", "client_correction_deadline", "lgu_submitted_at"])
                case.save(update_fields=update_fields)

                AuditLog.objects.create(
                    actor=request.user,
                    action="case_update",
                    target_object=f"Case: {case.tracking_id}",
                    details={"step": 2, "items": len(new_checklist), "uploads": upload_changes[:50]}
                )

                messages.success(request, "Checklist and uploads saved.")
                if case.status in {"not_received", "in_review"} and case.lgu_submitted_at is not None:
                    return redirect("case_detail", tracking_id=case.tracking_id)
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
            "rows": _build_checklist_rows(formset, docs, requirements=requirements),
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
        if case.status == "returned":
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
            form = CaseDetailsForm(request.POST, request.FILES, instance=case, user=request.user)
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

                return redirect("draft_wizard", draft_id=case.draft_id, step=2)
        else:
            form = CaseDetailsForm(instance=case, user=request.user)

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
                    "rows": _build_checklist_rows(formset, docs, requirements=requirements),
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
                            "rows": _build_checklist_rows(formset, docs, requirements=requirements),
                            "case_type_requirements": requirements,
                        })
                    seen.add(key)

                    uploaded_file = cd.get("file")
                    is_deleted = cd.get("is_deleted")
                    if is_deleted:
                        # User explicitly cleared/deleted the file for this doc_type
                        to_del = CaseDocument.objects.filter(case=case, doc_type=doc_type)
                        for d in to_del:
                            if d.file:
                                with contextlib.suppress(Exception):
                                    d.file.delete(save=False)
                            d.delete()
                    elif uploaded_file:
                        try:
                            _upsert_case_document(case=case, doc_type=doc_type, uploaded_file=uploaded_file, actor=request.user)
                        except ValueError as exc:
                            messages.error(request, str(exc))
                            docs = list(case.documents.all())
                            return render(request, "core/submit_case.html", {
                                "step": 2,
                                "formset": formset,
                                "case": case,
                                "is_edit": True,
                                "documents": docs,
                                "documents_by_type": {d.doc_type: d for d in docs},
                                "rows": _build_checklist_rows(formset, docs, requirements=requirements),
                                "case_type_requirements": requirements,
                            })

                    has_doc = CaseDocument.objects.filter(case=case, doc_type=doc_type).exists()
                    
                    is_custom = cd.get("doc_type") == "__custom__" or doc_type not in requirements
                    if is_custom and doc_type != "Endorsement Letter" and (is_deleted or not has_doc):
                        continue
                        
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
                
                # Cleanup: remove CaseDocument files that are no longer in the checklist
                current_doc_types = {item["doc_type"] for item in new_checklist}
                to_delete = CaseDocument.objects.filter(case=case).exclude(doc_type__in=current_doc_types)
                for d in to_delete:
                    if d.file:
                        with contextlib.suppress(Exception):
                            d.file.delete(save=False)
                    d.delete()

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
            "rows": _build_checklist_rows(formset, docs, requirements=requirements),
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

        # Backend validation: at least 1 document must be uploaded
        if CaseDocument.objects.filter(case=case).count() < 1:
            messages.error(request, "Please upload at least 1 document for this transaction.")
            return redirect("draft_wizard", draft_id=case.draft_id, step=3)

        if case.status != "client_correction":
            case.status = "not_received"
            
        case.lgu_submitted_at = timezone.now()

        # Priority: 1. case.area, 2. submitted_by.lgu_municipality
        effective_mun = (case.area or "").strip()
        if not effective_mun:
            effective_mun = getattr(getattr(case, "submitted_by", None), "lgu_municipality", "")

        if not (case.lgu_area_code or "").strip():
            case.lgu_area_code = _municipality_area_code(effective_mun)

        # Cleanup: remove CaseDocument files that are no longer in the checklist
        current_doc_types = {item["doc_type"] for item in checklist}
        to_delete = CaseDocument.objects.filter(case=case).exclude(doc_type__in=current_doc_types)
        for d in to_delete:
            if d.file:
                with contextlib.suppress(Exception):
                    d.file.delete(save=False)
            d.delete()

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

    with contextlib.suppress(Exception):
        _purge_expired_archived_case_documents(case=case)

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    if not is_ajax and request.user.role == "capitol_examiner" and case.assigned_to == request.user and case.status == "to_examine":
        old_status = case.status
        case.status = "in_review"
        case.save(update_fields=["status", "updated_at"])
        AuditLog.objects.create(
            actor=request.user,
            action="case_status_change",
            target_object=f"Case: {case.tracking_id}",
            details={"old_status": old_status, "new_status": "in_review", "note": "Examiner opened the case."},
        )

    can_edit = _lgu_can_edit_details(request.user, case)

    can_receive = (
        request.user.role == "capitol_receiving" and
        case.status in {"not_received", "client_correction"}
    )

    can_return = (
        request.user.role == "capitol_receiving" and
        case.status == "received" and
        case.assigned_to_id is None
    )

    can_assign = (
        request.user.role == "capitol_receiving" and
        case.status == "received" and
        case.assigned_to_id is None
    )

    has_submitted_correction = False
    if case.status == "client_correction" and case.lgu_submitted_at and case.returned_at:
        if case.lgu_submitted_at > case.returned_at:
            has_submitted_correction = True

    is_examiner = _is_examiner(request.user)
    is_receiver = _normalized_role(request.user) in {"capitol_receiving", "receiver"} or _normalized_role(request.user).endswith("_receiving")
    is_approver = _normalized_role(request.user) in {"capitol_approver", "approver"} or _normalized_role(request.user).endswith("_approver")
    is_taxmapper = _normalized_role(request.user) in {"capitol_taxmapper", "taxmapper"} or _normalized_role(request.user).endswith("_taxmapper")
    is_numberer = _normalized_role(request.user) in {"capitol_numberer", "numberer"} or _normalized_role(request.user).endswith("_numberer")
    is_releaser = _normalized_role(request.user) in {"capitol_releaser", "releaser"} or _normalized_role(request.user).endswith("_releaser")

    is_assigned_examiner = bool(case.assigned_to_id and case.assigned_to_id == request.user.id)
    is_assigned_taxmapper = bool(case.taxmapper_assigned_to_id and case.taxmapper_assigned_to_id == request.user.id)

    can_submit_for_approval = bool(case.status in {"to_examine", "in_review"} and is_assigned_examiner)
    can_return_to_receiving = bool(case.status in {"to_examine", "in_review"} and is_assigned_examiner)

    can_return_for_correction = bool(case.status == "for_approval" and is_approver)
    can_approve = bool(case.status == "for_approval" and is_approver)

    can_assign_taxmapper = bool(
        is_approver
        and case.status == "for_approval"
        and bool(getattr(case, "needs_taxmapping", False))
    )

    can_complete_taxmapping = bool(is_taxmapper and case.status == "for_taxmapping" and is_assigned_taxmapper)
    can_number = bool(is_numberer and case.status == "for_numbering")
    can_release = bool(is_releaser and case.status == "for_release")

    examiner_docs_blocked = bool(case.documents.exists() and case.documents.filter(reviewed_ok=False).exists())
    examiner_forward_reason = ""
    if case.status not in {"to_examine", "in_review"}:
        examiner_forward_reason = "This transaction is not in the Examiner stage."
    elif not is_assigned_examiner:
        assigned_to = getattr(case, "assigned_to", None)
        assigned_name = ""
        if assigned_to:
            assigned_name = (assigned_to.get_full_name() or getattr(assigned_to, "full_name", "") or getattr(assigned_to, "email", "") or "").strip()
        examiner_forward_reason = f"Assigned to {assigned_name}" if assigned_name else "This transaction is not assigned to you."
    elif examiner_docs_blocked:
        examiner_forward_reason = "Review all uploaded documents and mark them as checked before forwarding."

    can_return_to_receiving = bool(can_return_to_receiving)
    can_approve = bool(can_approve and not can_assign_taxmapper)

    can_reassign_examiner = bool(
        request.user.role == "super_admin"
        and case.status in {"to_examine", "in_review"}
        and case.assigned_to_id is not None
        and not case.documents.filter(reviewed_ok=True).exists()
    )

    examiners = None
    if can_assign or can_reassign_examiner:
        examiners = (
            CustomUser.objects.filter(role="capitol_examiner", is_active=True)
            .annotate(active_load=Count("assigned_cases", filter=Q(assigned_cases__status__in=["to_examine", "in_review"])))
            .order_by("active_load", "full_name", "email")
        )

    def _is_owner_for_internal_sections(user: CustomUser, case: Case) -> bool:
        role = getattr(user, "role", "") or ""
        if role == "super_admin":
            return True
        if role == "capitol_receiving":
            return case.status in {"not_received", "received", "client_correction"} and case.assigned_to_id is None
        if role == "capitol_examiner":
            return case.status in {"to_examine", "in_review"} and case.assigned_to_id == user.id
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
    show_internal = bool(is_capitol and _is_owner_for_internal_sections(request.user, case))
    role = (getattr(request.user, "role", "") or "").strip()

    show_correction_required_banner = False
    if getattr(case, "status", "") == "client_correction":
        show_correction_required_banner = True
    else:
        returned_by = getattr(case, "returned_by", None)
        returned_by_role = (getattr(returned_by, "role", "") or "").strip()
        if (
            returned_by_role == "capitol_approver"
            and getattr(case, "status", "") in {"to_examine", "in_review"}
            and getattr(case, "assigned_to_id", None) is not None
            and request.user.id == getattr(case, "assigned_to_id", None)
        ):
            show_correction_required_banner = True
        elif (
            returned_by_role == "capitol_examiner"
            and getattr(case, "status", "") == "received"
            and is_receiver
        ):
            show_correction_required_banner = True

    remarks = []
    history = []
    remark_form = None
    can_remark = False

    remarks_qs = CaseRemark.objects.filter(case=case).select_related("created_by")
    history_qs = (
        AuditLog.objects.filter(
            Q(target_object=f"Case: {case.tracking_id}") | 
            Q(target_object=f"Draft: {case.draft_id}")
        )
        .filter(action__in=["case_create", "case_update", "case_receipt", "case_assignment", "case_document_review", "case_status_change", "case_approval", "case_rejection", "case_numbered", "case_release", "case_remark"])
        .select_related("actor")
        .order_by("-created_at")
    )

    history = list(history_qs)
    for h in history:
        h.details_display = _format_case_history_details(getattr(h, "action", "") or "", getattr(h, "details", None))

    remarks = list(remarks_qs)

    if role == "super_admin":
        can_remark = True
    elif show_internal:
        can_remark = True
    elif role == "lgu_admin":
        can_remark = bool(
            getattr(case, "status", "") in {"draft", "not_received"}
            and getattr(case, "received_at", None) is None
            and getattr(case, "assigned_to_id", None) is None
            and _user_can_view_case(request.user, case)
        )

    if can_remark:
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
        "document_versions": list(DocumentVersion.objects.filter(case=case).order_by("-uploaded_at")),
        "archived_documents": list(ArchivedCaseDocument.objects.filter(case=case).order_by("-archived_at")[:200]),
        "is_examiner": is_examiner,
        "is_receiver": is_receiver,
        "is_approver": is_approver,
        "is_taxmapper": is_taxmapper,
        "is_numberer": is_numberer,
        "is_releaser": is_releaser,
        "can_edit": can_edit,
        "can_receive": can_receive,
        "can_return": can_return,
        "can_assign": can_assign,
        "has_submitted_correction": has_submitted_correction,
        "can_submit_for_approval": can_submit_for_approval,
        "examiner_docs_blocked": examiner_docs_blocked,
        "examiner_forward_reason": examiner_forward_reason,
        "can_return_to_receiving": can_return_to_receiving,
        "can_approve": can_approve,
        "can_return_for_correction": can_return_for_correction,
        "can_assign_taxmapper": can_assign_taxmapper,
        "taxmappers": taxmappers,
        "can_complete_taxmapping": can_complete_taxmapping,
        "can_number": can_number,
        "can_release": can_release,
        "can_reassign_examiner": can_reassign_examiner,
        "examiners": examiners,
        "case_numbers": case_numbers,
        "last_used_number": last_used_number,
        "suggested_next_number": suggested_next_number,
        "show_internal": show_internal,
        "show_correction_required_banner": show_correction_required_banner,
        "current_holder_label": _case_current_holder_label(case),
        "current_holder_detail": _case_current_holder_detail(case),
        "workflow_status_text": (
            "To be received by Receiver"
            if (getattr(case, "status", "") == "not_received" and getattr(case, "received_at", None) is None)
            else (
                "Currently with Receiver (Correction needed)" if getattr(case, "status", "") == "client_correction"
                else f"Currently with {_case_current_holder_label(case)}"
            )
        ),
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
def forward_for_approval(request, tracking_id):
    case = get_object_or_404(Case, tracking_id=tracking_id)
    
    # Auth Check: Only the assigned holder can forward it
    if getattr(request.user, "role", "") != "super_admin" and case.assigned_to_id != request.user.id:
        messages.error(request, "Unauthorized action.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.status not in {"to_examine", "in_review"}:
        messages.error(request, "This case is not eligible for approval submission.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    # Validation: Ensure all docs are checked (optional but recommended)
    if case.documents.filter(reviewed_ok=False).exists():
        messages.error(request, "Please check all documents before forwarding.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    old_status = case.status
    case.status = "for_approval"
    update_fields = ["status", "updated_at"]
    returned_by_role = getattr(getattr(case, "returned_by", None), "role", "") or ""
    if returned_by_role == "capitol_approver":
        case.return_reason = ""
        case.returned_at = None
        case.returned_by = None
        update_fields.extend(["return_reason", "returned_at", "returned_by"])
    case.save(update_fields=update_fields)

    AuditLog.objects.create(
        actor=request.user,
        action="case_status_change",
        target_object=f"Case: {case.tracking_id}",
        details={"old_status": old_status, "new_status": "for_approval", "note": "Examiner approved documents."}
    )

    messages.success(request, f"Case {case.tracking_id} forwarded for approval.")
    return redirect("submissions") # Redirect to workspace since it's no longer their task

@login_required
@require_POST
def add_case_remark(request, tracking_id):
    case = get_object_or_404(Case, tracking_id=tracking_id)
    role = getattr(request.user, "role", "") or ""

    # --- 1. Authorization Logic ---
    # We maintain your strict rules: users can only remark if the case is in their "hand"
    is_authorized = False

    if role == "super_admin":
        is_authorized = True
    elif role == "capitol_receiving":
        if case.status in {"not_received", "received"} and case.assigned_to_id is None and case.status != "client_correction":
            is_authorized = True
    elif role == "capitol_examiner":
        if case.status in {"to_examine", "in_review"} and case.assigned_to_id == request.user.id and case.status != "client_correction":
            is_authorized = True
    elif role == "capitol_approver":
        if case.status == "for_approval" and case.status != "client_correction":
            is_authorized = True
    elif role == "capitol_taxmapper":
        if case.status == "for_taxmapping" and case.taxmapper_assigned_to_id == request.user.id and case.status != "client_correction":
            is_authorized = True
    elif role == "capitol_numberer":
        if case.status == "for_numbering" and case.status != "client_correction":
            is_authorized = True
    elif role == "capitol_releaser":
        if case.status == "for_release" and case.status != "client_correction":
            is_authorized = True
    elif role == "lgu_admin":
        if (
            _user_can_view_case(request.user, case)
            and case.status in {"draft", "not_received"}
            and case.received_at is None
            and case.assigned_to_id is None
        ):
            is_authorized = True

    if not is_authorized:
        messages.error(request, "Not authorized to remark on this case right now.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    # --- 2. Form Processing ---
    # We use 'text' as the key to match your manual <textarea name="text">
    remark_content = request.POST.get("text", "").strip()
    
    if not remark_content:
        messages.error(request, "Please enter a valid remark.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    # --- 3. Data Persistence ---
    # Create the internal CaseRemark object
    CaseRemark.objects.create(
        case=case, 
        text=remark_content, 
        created_by=request.user
    )

    # Create the AuditLog entry (Fixing the previous 'remark_text' NameError)
    AuditLog.objects.create(
        actor=request.user,
        action="case_remark",
        target_object=f"Case: {case.tracking_id}",
        details={"remark": remark_content}
    )

    messages.success(request, "Internal note added.")
    return redirect("case_detail", tracking_id=case.tracking_id)


@login_required
@require_POST
def delete_case(request, tracking_id):
    denial = _require_super_admin(request)
    if denial:
        return denial

    case = get_object_or_404(Case, tracking_id=tracking_id)
    
    AuditLog.objects.create(
        actor=request.user,
        action="delete_case",
        target_object=f"Transaction Deleted: {case.tracking_id}",
        details={"client": case.client_name, "td_number": case.td_number}
    )
    
    # Delete associated document files from storage
    docs = list(case.documents.all())
    for d in docs:
        if d.file:
            with contextlib.suppress(Exception):
                d.file.delete(save=False)
    
    case.delete()
    messages.success(request, "Transaction has been permanently deleted.")
    return redirect("submissions")

@login_required
def submissions(request):
    if not (_is_capitol_staff(request.user) or request.user.role == "super_admin"):
        messages.error(request, "Not authorized.")
        return redirect("dashboard")

    scope = (request.GET.get("scope") or "me").strip().lower()
    tab = (request.GET.get("tab") or "").strip().lower()
    
    search = (request.GET.get("search") or "").strip()
    time_range = (request.GET.get("time_range") or "all").strip()
    lgu = (request.GET.get("lgu") or "all").strip()
    transaction_type = (request.GET.get("transaction_type") or "all").strip()
    date_from_raw = (request.GET.get("date_from") or "").strip()
    date_to_raw = (request.GET.get("date_to") or "").strip()

    def parse_safe_date(d_str):
        if not d_str:
            return None
        try:
            from datetime import datetime
            return datetime.strptime(d_str, '%Y-%m-%d').date()
        except ValueError:
            return None

    date_from = parse_safe_date(date_from_raw)
    date_to = parse_safe_date(date_to_raw)

    # Base: Everything submitted by LGUs
    qs = Case.objects.filter(lgu_submitted_at__isnull=False).select_related("submitted_by", "assigned_to", "returned_by").order_by("-created_at")

    examiners = None  # Will populate only if the user needs the Assignment Modal

    # ==========================================
    # SCOPE & TAB LOGIC
    # ==========================================
    if scope == "me" and request.user.role != "super_admin":
        page_title = "My Workspace"
        page_subtitle = "Cases specifically assigned to your queue."
        
        # 1. Scope Filter & Dynamic Tabs based on Role
        if request.user.role == "capitol_receiving":
            pending_intake_qs = qs.filter(status="not_received")
            to_assign_qs = qs.filter(status="received", assigned_to__isnull=True)
            received_qs = qs.filter(status="received")
            correction_qs = qs.filter(status="client_correction")
            returned_from_examiner_qs = qs.filter(status="received", assigned_to__isnull=True, returned_by__role="capitol_examiner")
            
            # For Receivers, "All Assigned" refers to the pool of cases awaiting intake, receipt, or assignment.
            # Once a case is assigned to an examiner, it is no longer in the Receiver's active workspace.
            active_assigned_qs = qs.filter(
                Q(status="not_received") |
                Q(status="received", assigned_to__isnull=True) |
                Q(status="client_correction")
            )

            tabs = [
                ("all_assigned", f"All Assigned ({active_assigned_qs.count()})"),
                ("pending", f"Pending ({pending_intake_qs.count()})"),
                ("received", f"Received ({received_qs.count()})"),
                ("to_assign", f"To Assign ({to_assign_qs.count()})"),
                ("correction", f"Under Correction ({correction_qs.count()})"),
                ("returned_from_examiner", f"Returned from Examiner ({returned_from_examiner_qs.count()})"),
            ]
            if not tab: tab = "all_assigned"

            # Fetch examiners to populate the Assign modal
            examiners = (
                CustomUser.objects.filter(role="capitol_examiner", is_active=True)
                .annotate(active_load=Count("assigned_cases", filter=Q(assigned_cases__status__in=["to_examine", "in_review"])))
                .order_by("active_load", "full_name", "email")
            )

            if tab == "pending":
                qs = pending_intake_qs
            elif tab == "received":
                qs = received_qs
            elif tab == "to_assign":
                qs = to_assign_qs
            elif tab == "correction":
                qs = correction_qs
            elif tab == "returned_from_examiner":
                qs = returned_from_examiner_qs
            else:
                # Strictly assigned to this specific user AND currently active
                qs = active_assigned_qs
            
        elif request.user.role == "capitol_examiner":
            to_examine_qs = qs.filter(assigned_to=request.user, status="to_examine")
            under_review_qs = qs.filter(assigned_to=request.user, status="in_review")
            returned_qs = qs.filter(assigned_to=request.user, returned_by__role="capitol_approver")
            
            # Show all handled Cases by the Examiner (any status, if assigned to them)
            active_assigned_qs = qs.filter(assigned_to=request.user)

            tabs = [
                ("all_assigned", f"All Assigned ({active_assigned_qs.count()})"), 
                ("to_examine", f"To Examine ({to_examine_qs.count()})"), 
                ("under_review", f"Under Review ({under_review_qs.count()})"), 
                ("returned", f"Returned ({returned_qs.count()})")
            ]
            if not tab: tab = "all_assigned"
            
            if tab == "to_examine":
                qs = to_examine_qs
            elif tab == "under_review":
                qs = under_review_qs
            elif tab == "returned":
                qs = returned_qs.filter(status="in_review")
            else:
                # Strictly assigned to this specific user AND currently active
                qs = active_assigned_qs
                
        elif request.user.role == "capitol_approver":
            to_approve_qs = qs.filter(status="for_approval")
            
            from django.db.models.functions import Replace
            from django.db.models import Value
            approved_tids_sq = AuditLog.objects.filter(
                actor=request.user,
                action="case_approval"
            ).annotate(
                tid=Replace("target_object", Value("Case: "), Value(""))
            ).values("tid")

            approved_qs = qs.filter(tracking_id__in=approved_tids_sq)
            returned_to_examiner_qs = qs.filter(status="in_review", returned_by=request.user)
            
            # Approvers share a queue for 'to_approve', and their own approved cases
            active_assigned_qs = (to_approve_qs | approved_qs).distinct()

            tabs = [
                ("all_assigned", f"All Assigned ({active_assigned_qs.count()})"),
                ("to_approve", f"To Approve ({to_approve_qs.count()})"),
                ("approved", f"Approved ({approved_qs.count()})"),
                ("returned_to_examiner", f"Returned to Examiner ({returned_to_examiner_qs.count()})"),
            ]
            if not tab: tab = "all_assigned"

            if tab == "to_approve":
                qs = to_approve_qs
            elif tab == "approved":
                qs = approved_qs
            elif tab == "returned_to_examiner":
                qs = returned_to_examiner_qs
            else:
                # For Approvers, All Assigned shows everything pending approval
                qs = active_assigned_qs
            
        elif request.user.role == "capitol_taxmapper":
            pending_taxmapping_qs = qs.filter(status="for_taxmapping")
            
            # Use specific taxmapper assignment field
            active_assigned_qs = qs.filter(taxmapper_assigned_to=request.user, status="for_taxmapping")

            tabs = [
                ("all_assigned", f"All Assigned ({active_assigned_qs.count()})"),
                ("pending_taxmapping", f"Pending Taxmapping ({pending_taxmapping_qs.count()})")
            ]
            if not tab: tab = "all_assigned"
            
            if tab == "pending_taxmapping":
                qs = pending_taxmapping_qs
            else:
                # Strictly assigned to this specific tax mapper AND currently active
                qs = active_assigned_qs

        elif request.user.role == "capitol_numberer":
            pending_numbering_qs = qs.filter(status="for_numbering")
            numbered_qs = qs.exclude(td_number__isnull=True).exclude(td_number="")
            
            # Numberers share a queue for numbering
            active_assigned_qs = pending_numbering_qs

            tabs = [
                ("all_assigned", f"All Assigned ({active_assigned_qs.count()})"),
                ("pending_numbering", f"Pending Numbering ({pending_numbering_qs.count()})"),
                ("numbered", f"Numbered ({numbered_qs.count()})")
            ]
            if not tab: tab = "all_assigned"
            
            if tab == "pending_numbering":
                qs = pending_numbering_qs
            elif tab == "numbered":
                qs = numbered_qs
            else:
                # For Numberers, All Assigned shows everything pending numbering
                qs = active_assigned_qs
                
        elif request.user.role == "capitol_releaser":
            pending_release_qs = qs.filter(status="for_release")
            released_qs = qs.filter(status="released")
            
            # Releasers share a queue for release
            active_assigned_qs = pending_release_qs

            tabs = [
                ("all_assigned", f"All Assigned ({active_assigned_qs.count()})"),
                ("pending_release", f"Pending Release ({pending_release_qs.count()})"),
                ("released", f"Released ({released_qs.count()})")
            ]
            if not tab: tab = "all_assigned"
            
            if tab == "pending_release":
                qs = pending_release_qs
            elif tab == "released":
                qs = released_qs
            else:
                # For Releasers, All Assigned shows everything pending release
                qs = active_assigned_qs

    else:
        # GLOBAL TRANSACTIONS SCOPE
        page_title = "All Transactions"
        page_subtitle = "Global view of all submitted cases."
        
        tabs = [
            ("all", "All"), ("pending", "Pending"), ("received", "Received"),
            ("to_examine", "To Examine"), ("for_taxmapping", "For Taxmapping"),
            ("for_approval", "For Approval"), ("for_numbering", "For Numbering"),
            ("for_release", "For Release"), ("released", "Released"),
        ]
        
        tab_map = {
            "pending": ["not_received", "client_correction"],
            "received": ["received"],
            "to_examine": ["to_examine", "in_review"],
            "for_taxmapping": ["for_taxmapping"],
            "for_approval": ["for_approval"],
            "for_numbering": ["for_numbering"],
            "for_release": ["for_release"],
            "released": ["released"],
        }

        # CRITICAL FIX: Update the 'qs' based on the selected tab
        if tab in tab_map:
            qs = qs.filter(status__in=tab_map[tab])
            
            # Additional logic for 'received' tab to show only unassigned ones
            if tab == "received":
                qs = qs.filter(assigned_to__isnull=True)
    # ==========================================
    # SEARCH & FILTERS
    # ==========================================
    if search:
        qs = qs.filter(
            Q(tracking_id__icontains=search) |
            Q(client_name__icontains=search) |
            Q(client_first_name__icontains=search) |
            Q(client_last_name__icontains=search) |
            Q(client_email__icontains=search) |
            Q(submitted_by__email__icontains=search)
        )

    today = timezone.localtime(timezone.now()).date()
    if time_range == 'today':
        qs = qs.filter(created_at__date=today)
    elif time_range == 'this_week':
        start = today - timedelta(days=today.weekday())
        qs = qs.filter(created_at__date__gte=start)
    elif time_range == 'this_month':
        qs = qs.filter(
            created_at__year=today.year,
            created_at__month=today.month
        )
    elif time_range == 'this_year':
        qs = qs.filter(created_at__year=today.year)

    if lgu and lgu != 'all':
        qs = qs.filter(area__iexact=lgu)

    if transaction_type and transaction_type != 'all':
        qs = qs.filter(case_type__iexact=transaction_type)

    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)

    number_q = (request.GET.get("number") or "").strip()
    if number_q:
        qs = qs.filter(Q(td_number__icontains=number_q) | Q(tracking_id__icontains=number_q)).distinct()

    lgu_list = [choice[0] for choice in getattr(CustomUser, "LGU_MUNICIPALITY_CHOICES", [])]
    
    db_type_list = Case.objects.exclude(case_type='').values_list('case_type', flat=True).distinct().order_by('case_type')
    type_list = [(t, dict(Case.CASE_TYPE_CHOICES).get(t, t)) for t in db_type_list]

    from django.db.models import Subquery, OuterRef, Value
    from django.db.models.functions import Concat
    approver_sq = AuditLog.objects.filter(
        action="case_approval",
        target_object=Concat(Value("Case: "), OuterRef("tracking_id"))
    ).order_by("-created_at").values("actor__full_name")[:1]
    
    qs = qs.annotate(approver_name=Subquery(approver_sq))

    # Preserve parameters for pagination
    query = request.GET.copy()
    with contextlib.suppress(Exception): query.pop("page")
    
    query_no_tab = query.copy()
    with contextlib.suppress(Exception): query_no_tab.pop("tab")

    paginator = Paginator(qs, 15)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    return render(request, "core/submissions.html", {
        "role_display": request.user.get_role_display(),
        "page_obj": page_obj,
        "tab": tab,
        "scope": scope,
        "page_title": page_title,
        "page_subtitle": page_subtitle,
        "tabs": tabs,
        "lgu_list": lgu_list,
        "type_list": type_list,
        "selected_search": search,
        "selected_time_range": time_range,
        "selected_lgu": lgu,
        "selected_transaction_type": transaction_type,
        "selected_date_from": date_from_raw,
        "selected_date_to": date_to_raw,
        "qs_params": query.urlencode(),
        "qs_params_no_tab": query_no_tab.urlencode(),
        "examiners": examiners,  # Injected here for the modal logic
    })

@login_required
def audit_logs(request):
    # Allow Super Admins, LGU Admins, AND Capitol Staff
    if request.user.role not in ['super_admin', 'lgu_admin'] and not getattr(request.user, 'role', '').startswith('capitol_'):
        messages.error(request, "Not authorized.")
        return redirect("dashboard")

    # Super Admins see everything. Everyone else sees ONLY their own activity.
    if request.user.role == 'super_admin':
        qs = AuditLog.objects.select_related("actor", "target_user").all()
    else:
        qs = AuditLog.objects.filter(actor=request.user).select_related("actor", "target_user")

    action = (request.GET.get("action") or "").strip()
    q = (request.GET.get("q") or "").strip()

    if action: qs = qs.filter(action=action)
    if q:
        qs = qs.filter(
            Q(target_object__icontains=q) |
            Q(actor__email__icontains=q) |
            Q(target_user__email__icontains=q)
        )

    paginator = Paginator(qs.order_by("-created_at"), 25)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    current = int(getattr(page_obj, "number", 1) or 1)
    num_pages = int(getattr(paginator, "num_pages", 1) or 1)
    start = max(1, current - 5)
    end = min(num_pages, current + 5)
    page_window = list(range(start, end + 1))

    return render(request, "core/audit_logs.html", {
        "role_display": request.user.get_role_display(),
        "page_obj": page_obj,
        "action_filter": action,
        "q_filter": q,
        "actions": AuditLog.ACTION_CHOICES,
        "page_window": page_window,
        "num_pages": num_pages,
    })


@login_required
@require_POST
def receive_case(request, tracking_id):
    case = get_object_or_404(Case, tracking_id=tracking_id)

    if request.user.role != "capitol_receiving":
        messages.error(request, "Only Capitol Receiver can receive cases.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.status not in {"not_received", "client_correction"}:
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

    date_received = case.received_at.strftime('%B %d, %Y') if case.received_at else timezone.now().strftime('%B %d, %Y')
    html_message = f"""<p>Dear {case.client_display_name},</p>
<p>This is to formally inform you that your real property tax declaration 
request has been successfully received by the Provincial Assessor's 
Office of Cebu.</p>
<p>Case Reference No.: <b>{case.tracking_id}</b><br>
Date Received: <b>{date_received}</b><br>
Current Status: <b>Received</b></p>
<p>Your submitted documents are now in queue for examination by our 
designated Capitol staff. You will be notified of any further updates 
as your case progresses through the processing workflow.</p>
<p>To monitor the status of your case at any time, please visit:<br>
<a href="https://pastrack.onrender.com/">https://pastrack.onrender.com/</a><br>
and enter your Case Reference Number in the tracking portal.</p>
<p>Should you have any concerns, please do not hesitate to contact the 
Provincial Assessor's Office of Cebu.</p>
<p>Salamat ug padayon ang inyong pagtamod sa among serbisyo.</p>
<p>Respectfully,<br>
Provincial Assessor's Office<br>
Province of Cebu<br>
PAStrack Document Tracking System</p>"""

    plain_message = f"""Dear {case.client_display_name},

This is to formally inform you that your real property tax declaration 
request has been successfully received by the Provincial Assessor's 
Office of Cebu.

Case Reference No.: {case.tracking_id}
Date Received:      {date_received}
Current Status:     Received

Your submitted documents are now in queue for examination by our 
designated Capitol staff. You will be notified of any further updates 
as your case progresses through the processing workflow.

To monitor the status of your case at any time, please visit:
https://pastrack.onrender.com/
and enter your Case Reference Number in the tracking portal.

Should you have any concerns, please do not hesitate to contact the 
Provincial Assessor's Office of Cebu.

Salamat ug padayon ang inyong pagtamod sa among serbisyo.

Respectfully,
Provincial Assessor's Office
Province of Cebu
PAStrack Document Tracking System"""

    send_case_email(
        to_email=(case.client_email or "").strip(),
        subject=f"PAStrack Update: Submission Received ({case.tracking_id})",
        message=(
            f"Dear Client,\n\n"
            f"Your transaction has been successfully received by the Provincial Assessor's Office and is now awaiting review.\n\n"
            f"Transaction Details:\n"
            f"• Tracking ID: {case.tracking_id}\n"
            f"• Current Status: {dict(Case.STATUS_CHOICES).get(case.status, case.status)}\n\n"
            f"You may monitor the progress of your transaction through the PASTrack portal using your tracking ID.\n\n"
            f"For transaction tracking, FAQs, and service information, please visit our website.\n\n"
            f"If you require assistance, please contact the Provincial Assessor's Office.\n\n"
            f"Thank you for using PASTrack."
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

    if case.status != "received":
        messages.error(request, "Only received cases can be returned to the client.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.assigned_to_id is not None:
        messages.error(request, "This case is assigned and cannot be returned right now.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "Return reason is required.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    # Preserve the Examiner's feedback if it exists
    if case.return_reason:
        reason = f"{reason}\n\n---\n\nExaminer Feedback:\n{case.return_reason}"

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
        details={"new_status": case.status, "reason": reason, "deadline": case.client_correction_deadline.isoformat() if case.client_correction_deadline else None, "returned_to": "Client"}
    )

    date_returned = case.returned_at.strftime('%B %d, %Y') if case.returned_at else timezone.now().strftime('%B %d, %Y')
    html_message = f"""<p>Dear {case.client_display_name},</p>
<p>This is to formally notify you that your real property tax declaration 
request has been reviewed and returned for correction by the Provincial 
Assessor's Office of Cebu.</p>
<p>Case Reference No.: <b>{case.tracking_id}</b><br>
Date Returned: <b>{date_returned}</b><br>
Current Status: <b>Returned for Correction</b></p>
<p>Reason / Remarks:<br>
{reason}</p>
<p>Your designated LGU Administrator has been notified and will coordinate 
with you regarding the necessary corrections. Please ensure that the 
required revisions are addressed promptly to avoid further delays in 
the processing of your case.</p>
<p>Once the corrections have been made and the case is resubmitted, you 
will receive a confirmation notification.</p>
<p>To monitor the status of your case at any time, please visit:<br>
<a href="https://pastrack.onrender.com/">https://pastrack.onrender.com/</a><br>
and enter your Case Reference Number in the tracking portal.</p>
<p>Should you have any concerns, please do not hesitate to contact the 
Provincial Assessor's Office of Cebu or your municipal LGU 
Assessor's Office.</p>
<p>Salamat ug padayon ang inyong pagtamod sa among serbisyo.</p>
<p>Respectfully,<br>
Provincial Assessor's Office<br>
Province of Cebu<br>
PAStrack Document Tracking System</p>"""

    plain_message = f"""Dear {case.client_display_name},

This is to formally notify you that your real property tax declaration 
request has been reviewed and returned for correction by the Provincial 
Assessor's Office of Cebu.

Case Reference No.: {case.tracking_id}
Date Returned:      {date_returned}
Current Status:     Returned for Correction

Reason / Remarks:
{reason}

Your designated LGU Administrator has been notified and will coordinate 
with you regarding the necessary corrections. Please ensure that the 
required revisions are addressed promptly to avoid further delays in 
the processing of your case.

Once the corrections have been made and the case is resubmitted, you 
will receive a confirmation notification.

To monitor the status of your case at any time, please visit:
https://pastrack.onrender.com/
and enter your Case Reference Number in the tracking portal.

Should you have any concerns, please do not hesitate to contact the 
Provincial Assessor's Office of Cebu or your municipal LGU 
Assessor's Office.

Salamat ug padayon ang inyong pagtamod sa among serbisyo.

Respectfully,
Provincial Assessor's Office
Province of Cebu
PAStrack Document Tracking System"""

    email_ok = send_case_email(
        to_email=(case.client_email or "").strip(),
        subject=f"PAStrack Update: Additional Action Required ({case.tracking_id})",
        message=(
            f"Dear Client,\n\n"
            f"Your transaction requires additional action before processing can continue.\n\n"
            f"Transaction Details:\n"
            f"• Tracking ID: {case.tracking_id}\n"
            f"• Current Status: Returned for Correction\n\n"
            f"Remarks:\n"
            f"{reason}\n\n"
            f"Correction Deadline: {case.client_correction_deadline}\n\n"
            f"Please review the remarks and submit the required corrections before the stated deadline.\n\n"
            f"You may track your transaction and review updates through the PASTrack portal.\n\n"
            f"For FAQs and assistance, please contact the Provincial Assessor's Office.\n\n"
            f"Thank you."
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
def assign_case(request, tracking_id):
    # 1. Fetch the case at the very beginning so it always exists for this function
    case = get_object_or_404(Case, tracking_id=tracking_id)

    if request.user.role != "capitol_receiving":
        messages.error(request, "Only Receiver can assign cases.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if request.method == "POST":
        if case.status not in {"received", "client_correction"} or case.assigned_to_id is not None:
            messages.error(request, "This case is not eligible for assignment.")
            return redirect("case_detail", tracking_id=case.tracking_id)

        # File check before assignment
        if case.documents.count() == 0:
            messages.error(request, "Please attach files for the Documents Checklist of the Transaction.")
            return redirect("case_detail", tracking_id=case.tracking_id)

        examiner_id = request.POST.get("assigned_to")
        
        # Prevent 404/Error if the user clicked 'Confirm' without picking an examiner
        if not examiner_id:
            messages.error(request, "Please select an examiner before confirming.")
            # Use META referer to send them back to exactly where they were
            return redirect(request.META.get('HTTP_REFERER', 'dashboard'))
            
        # Fetch the selected examiner staff account
        examiner = get_object_or_404(CustomUser, id=examiner_id, role="capitol_examiner", is_active=True)
        
        # --- WORKFLOW TRANSITION ---
        # We assign the user AND update the status to push it to the next phase
        case.assigned_to = examiner
        case.status = "to_examine" 
        case.assigned_at = timezone.now()
        case.save(update_fields=["assigned_to", "assigned_at", "status", "updated_at"])
        
        # Create Audit Log for transparency
        AuditLog.objects.create(
            actor=request.user,
            action="case_assignment",
            target_object=f"Case: {case.tracking_id}",
            details={
                "new_status": case.status,
                "assigned_to": f"{examiner.get_full_name()} - {examiner.get_role_display()}",
            }
        )

        messages.success(request, f"Case {case.tracking_id} successfully assigned to {examiner.get_full_name()}.")
        
        # After assigning, redirecting to case_detail will now show the 
        # "Assigned" status and lock the controls for the Receiver.
        return redirect("case_detail", tracking_id=case.tracking_id)

    # 2. Fallback: If it's a GET request (e.g. manual URL entry), return to dashboard
    return redirect('dashboard')


@login_required
@require_POST
def reassign_case_examiner(request, tracking_id):
    if request.user.role != "super_admin":
        messages.error(request, "Not authorized.")
        return redirect("dashboard")

    case = get_object_or_404(Case, tracking_id=tracking_id)
    if case.status not in {"to_examine", "in_review"} or case.assigned_to_id is None:
        messages.error(request, "This transaction is not assigned to an Examiner.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.documents.filter(reviewed_ok=True).exists():
        messages.error(request, "Cannot reassign: the current Examiner already marked at least one document as Reviewed OK.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    examiner_id = (request.POST.get("assigned_to") or "").strip()
    if not examiner_id.isdigit():
        messages.error(request, "Please select an Examiner.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    new_examiner = get_object_or_404(CustomUser, id=int(examiner_id), role="capitol_examiner", is_active=True)
    if case.assigned_to_id == new_examiner.id:
        messages.error(request, "This transaction is already assigned to that Examiner.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    old_examiner = case.assigned_to
    case.assigned_to = new_examiner
    case.assigned_at = timezone.now()
    case.save(update_fields=["assigned_to", "assigned_at", "updated_at"])

    AuditLog.objects.create(
        actor=request.user,
        action="case_assignment",
        target_object=f"Case: {case.tracking_id}",
        details={
            "reassigned": True,
            "from": (old_examiner.get_full_name() if old_examiner else ""),
            "to": (new_examiner.get_full_name() or ""),
        },
    )

    messages.success(request, f"Transaction {case.tracking_id} reassigned to {new_examiner.get_full_name()}.")
    return redirect("case_detail", tracking_id=case.tracking_id)
    
@login_required
@require_POST
def submit_for_approval(request, tracking_id):
    case = get_object_or_404(Case, tracking_id=tracking_id)

    if getattr(request.user, "role", "") != "super_admin" and case.assigned_to_id != request.user.id:
        messages.error(request, "Only the assigned Examiner can submit this case for approval.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.status not in {"to_examine", "in_review"} or case.assigned_to_id != request.user.id:
        messages.error(request, "This case is not eligible for approval submission.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.documents.exists() and case.documents.filter(reviewed_ok=False).exists():
        messages.error(request, "Review all uploaded documents and mark them as checked before submitting for approval.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    old_status = case.status
    case.status = "for_approval"
    update_fields = ["status", "updated_at"]
    returned_by_role = getattr(getattr(case, "returned_by", None), "role", "") or ""
    if returned_by_role == "capitol_approver":
        case.return_reason = ""
        case.returned_at = None
        case.returned_by = None
        update_fields.extend(["return_reason", "returned_at", "returned_by"])
    case.save(update_fields=update_fields)

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

    if (request.POST.get("confirm_approve") or "").strip() != "1":
        messages.error(request, "Approval confirmation is required.")
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

    date_approved = case.updated_at.strftime('%B %d, %Y') if case.updated_at else timezone.now().strftime('%B %d, %Y')
    html_message = f"""<p>Dear {case.client_display_name},</p>
<p>We are pleased to inform you that your real property tax declaration 
request has been reviewed and officially approved by the Provincial 
Assessor's Office of Cebu.</p>
<p>Case Reference No.: <b>{case.tracking_id}</b><br>
Date Approved: <b>{date_approved}</b><br>
Current Status: <b>For Numbering</b></p>
<p>Your case is now being processed for the issuance of the official 
Tax Declaration number. You will receive a final notification once 
your documents are ready for release.</p>
<p>To monitor the status of your case at any time, please visit:<br>
<a href="https://pastrack.onrender.com/">https://pastrack.onrender.com/</a><br>
and enter your Case Reference Number in the tracking portal.</p>
<p>Should you have any concerns, please do not hesitate to contact the 
Provincial Assessor's Office of Cebu.</p>
<p>Salamat ug padayon ang inyong pagtamod sa among serbisyo.</p>
<p>Respectfully,<br>
Provincial Assessor's Office<br>
Province of Cebu<br>
PAStrack Document Tracking System</p>"""

    plain_message = f"""Dear {case.client_display_name},

We are pleased to inform you that your real property tax declaration 
request has been reviewed and officially approved by the Provincial 
Assessor's Office of Cebu.

Case Reference No.: {case.tracking_id}
Date Approved:      {date_approved}
Current Status:     For Numbering

Your case is now being processed for the issuance of the official 
Tax Declaration number. You will receive a final notification once 
your documents are ready for release.

To monitor the status of your case at any time, please visit:
https://pastrack.onrender.com/
and enter your Case Reference Number in the tracking portal.

Should you have any concerns, please do not hesitate to contact the 
Provincial Assessor's Office of Cebu.

Salamat ug padayon ang inyong pagtamod sa among serbisyo.

Respectfully,
Provincial Assessor's Office
Province of Cebu
PAStrack Document Tracking System"""

    send_case_email(
        to_email=(case.client_email or "").strip(),
        subject=f"PAStrack Update: Transaction Approved ({case.tracking_id})",
        message=(
            f"Dear Client,\n\n"
            f"Your transaction has successfully passed review and has been approved for further processing.\n\n"
            f"Transaction Details:\n"
            f"• Tracking ID: {case.tracking_id}\n"
            f"• Current Status: {dict(Case.STATUS_CHOICES).get(case.status, case.status)}\n\n"
            f"No action is required from you at this time.\n\n"
            f"You may continue monitoring your transaction through the PASTrack portal.\n\n"
            f"For FAQs, updates, and assistance, please visit our website or contact the Provincial Assessor's Office.\n\n"
            f"Thank you for your patience and cooperation."
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

    flagged_docs = [d for d in case.documents.all() if (d.review_remark or "").strip()]
    if flagged_docs:
        # Append the list of flagged files to the return reason so the Examiner sees it clearly
        flagged_details = "\n\nFlagged Documents by Approver:\n" + "\n".join(
            f"- {d.doc_type}: {d.review_remark}" for d in flagged_docs
        )
        reason += flagged_details

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

    if case.status not in {"to_examine", "in_review"} or case.assigned_to_id != request.user.id:
        messages.error(request, "This case is not eligible for return to Receiving.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    reason = (request.POST.get("reason") or "").strip()
    flagged = (request.POST.get("flagged") or "").strip()
    
    if flagged:
        reason = f"{reason}\n\n{flagged}"
        
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
            "returned_to": "Receiver",
            "unchecked_documents": list(case.documents.filter(reviewed_ok=False).values_list("doc_type", flat=True)[:50]),
        }
    )

    messages.success(request, f"Case {case.tracking_id} returned to Receiving.")
    return redirect("case_detail", tracking_id=case.tracking_id)


@login_required
@require_POST
def mark_numbered(request, tracking_id):
    case = get_object_or_404(Case, tracking_id=tracking_id)

    if request.user.role not in {"capitol_numberer", "super_admin"}:
        messages.error(request, "Unauthorized.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if request.user.role != "super_admin":
        if case.status != "for_numbering":
            messages.error(request, "This case is not eligible for numbering.")
            return redirect("case_detail", tracking_id=case.tracking_id)

        if case.documents.exists() and case.documents.filter(reviewed_ok=False).exists():
            messages.error(request, "Review all uploaded documents and mark them as checked before numbering.")
            return redirect("case_detail", tracking_id=case.tracking_id)

    transaction_number = (request.POST.get("transaction_number") or request.POST.get("numbers") or "").strip()
    if not transaction_number:
        messages.error(request, "Transaction Number is required.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if not transaction_number.isdigit() or len(transaction_number) != 6:
        messages.error(request, "Transaction Number must be exactly 6 digits (numbers only).")
        return redirect("case_detail", tracking_id=case.tracking_id)

    old_status = case.status
    old_transaction_number = (case.td_number or "").strip()

    if old_transaction_number and request.user.role != "super_admin":
        messages.error(request, "Transaction Number is already set and cannot be overridden.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    case.td_number = transaction_number
    update_fields = ["td_number", "updated_at"]
    if case.status == "for_numbering":
        case.status = "for_release"
        update_fields.append("status")
    try:
        case.save(update_fields=update_fields)
    except IntegrityError:
        return redirect(
            reverse("case_detail", kwargs={"tracking_id": case.tracking_id})
            + "?duplicate_error=1"
        )

    AuditLog.objects.create(
        actor=request.user,
        action="case_numbered",
        target_object=f"Case: {case.tracking_id}",
        details={
            "old_status": old_status,
            "new_status": case.status,
            "transaction_number": transaction_number,
            "previous_transaction_number": old_transaction_number,
        }
    )

    if old_status == "for_numbering":
        messages.success(request, f"Transaction Number saved. Case {case.tracking_id} moved to For Release.")
    else:
        messages.success(request, "Transaction Number updated.")
    return redirect("case_detail", tracking_id=case.tracking_id)


@login_required
@require_POST
def transaction_corrected(request, tracking_id):
    """Capitol Receiver marks a client_correction case as corrected and re-receives it."""
    case = get_object_or_404(Case, tracking_id=tracking_id)

    if request.user.role != "capitol_receiving":
        messages.error(request, "Only Receiver can mark a case as corrected.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    if case.status != "client_correction":
        messages.error(request, "This case is not in the correction state.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    old_status = case.status
    case.status = "received"
    case.return_reason = ""
    case.save(update_fields=["status", "updated_at", "return_reason"])

    AuditLog.objects.create(
        actor=request.user,
        action="case_status_change",
        target_object=f"Case: {case.tracking_id}",
        details={"old_status": old_status, "new_status": "received", "note": "Receiver marked correction as complete."},
    )

    html_message = f"""<p>Dear {case.client_display_name},</p>
<p>We are pleased to inform you that the corrected documents for your real 
property tax declaration request have been successfully received and 
accepted by the Provincial Assessor's Office of Cebu.</p>
<p>Case Reference No.: <b>{case.tracking_id}</b><br>
Current Status: <b>Received</b></p>
<p>Your submitted documents are now back in queue for examination by our 
designated Capitol staff. You will be notified of any further updates 
as your case progresses through the processing workflow.</p>
<p>To monitor the status of your case at any time, please visit:<br>
<a href="https://pastrack.onrender.com/">https://pastrack.onrender.com/</a><br>
and enter your Case Reference Number in the tracking portal.</p>
<p>Should you have any concerns, please do not hesitate to contact the 
Provincial Assessor's Office of Cebu or your municipal LGU 
Assessor's Office.</p>
<p>Salamat ug padayon ang inyong pagtamod sa among serbisyo.</p>
<p>Respectfully,<br>
Provincial Assessor's Office<br>
Province of Cebu<br>
PAStrack Document Tracking System</p>"""

    plain_message = f"""Dear {case.client_display_name},

We are pleased to inform you that the corrected documents for your real 
property tax declaration request have been successfully received and 
accepted by the Provincial Assessor's Office of Cebu.

Case Reference No.: {case.tracking_id}
Current Status:     Received

Your submitted documents are now back in queue for examination by our 
designated Capitol staff. You will be notified of any further updates 
as your case progresses through the processing workflow.

To monitor the status of your case at any time, please visit:
https://pastrack.onrender.com/
and enter your Case Reference Number in the tracking portal.

Should you have any concerns, please do not hesitate to contact the 
Provincial Assessor's Office of Cebu or your municipal LGU 
Assessor's Office.

Salamat ug padayon ang inyong pagtamod sa among serbisyo.

Respectfully,
Provincial Assessor's Office
Province of Cebu
PAStrack Document Tracking System"""

    send_case_email(
        to_email=(case.client_email or "").strip(),
        subject=f"PAStrack — Case {case.tracking_id} Corrections Received",
        message=plain_message,
        html_message=html_message,
    )

    messages.success(request, f"Case {case.tracking_id} marked as corrected and re-received.")
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

    claimed_by_name = request.POST.get("claimed_by_name", "").strip()
    claimed_by_contact = request.POST.get("claimed_by_contact", "").strip()

    if not claimed_by_name or not claimed_by_contact:
        messages.error(request, "Both Claimant Name and Contact Number are required.")
        return redirect("case_detail", tracking_id=case.tracking_id)

    old_status = case.status
    case.status = "released"
    case.released_at = timezone.now()
    case.claimed_by_name = claimed_by_name
    case.claimed_by_contact = claimed_by_contact
    case.save(update_fields=["status", "released_at", "claimed_by_name", "claimed_by_contact", "updated_at"])
    with contextlib.suppress(Exception):
        _purge_all_archived_case_documents(case=case)

    AuditLog.objects.create(
        actor=request.user,
        action="case_release",
        target_object=f"Case: {case.tracking_id}",
        details={"old_status": old_status, "new_status": case.status}
    )

    date_released = case.released_at.strftime('%B %d, %Y') if case.released_at else timezone.now().strftime('%B %d, %Y')
    html_message = f"""<p>Dear {case.client_display_name},</p>
<p>We are pleased to inform you that your real property tax declaration 
request has been fully processed and is now ready for release by the 
Provincial Assessor's Office of Cebu.</p>
<p>Case Reference No.: <b>{case.tracking_id}</b><br>
Tax Declaration No.: <b>{case.td_number or 'N/A'}</b><br>
Date Released: <b>{date_released}</b><br>
Current Status: <b>Released</b></p>
<p>You or your authorized representative may now claim your official 
Tax Declaration documents at the Provincial Assessor's Office. 
Please bring a valid government-issued identification card and 
your Case Reference Number upon claiming.</p>
<p>Office Address:<br>
Provincial Assessor's Office<br>
Cebu Provincial Capitol, Cebu City</p>
<p>Office Hours:<br>
Monday to Friday | 8:00 AM – 5:00 PM<br>
(Except Public Holidays)</p>
<p>To verify the status of your case, please visit:<br>
<a href="https://pastrack.onrender.com/">https://pastrack.onrender.com/</a><br>
and enter your Case Reference Number in the tracking portal.</p>
<p>Salamat ug padayon ang inyong pagtamod sa among serbisyo.</p>
<p>Respectfully,<br>
Provincial Assessor's Office<br>
Province of Cebu<br>
PAStrack Document Tracking System</p>"""

    plain_message = f"""Dear {case.client_display_name},

We are pleased to inform you that your real property tax declaration 
request has been fully processed and is now ready for release by the 
Provincial Assessor's Office of Cebu.

Case Reference No.:      {case.tracking_id}
Tax Declaration No.:     {case.td_number or 'N/A'}
Date Released:           {date_released}
Current Status:          Released

You or your authorized representative may now claim your official 
Tax Declaration documents at the Provincial Assessor's Office. 
Please bring a valid government-issued identification card and 
your Case Reference Number upon claiming.

Office Address:
Provincial Assessor's Office
Cebu Provincial Capitol, Cebu City

Office Hours:
Monday to Friday | 8:00 AM – 5:00 PM
(Except Public Holidays)

To verify the status of your case, please visit:
https://pastrack.onrender.com/
and enter your Case Reference Number in the tracking portal.

Salamat ug padayon ang inyong pagtamod sa among serbisyo.

Respectfully,
Provincial Assessor's Office
Province of Cebu
PAStrack Document Tracking System"""

    send_case_email(
        to_email=(case.client_email or "").strip(),
        subject=f"PAStrack Update: Documents Ready for Release ({case.tracking_id})",
        message=(
            f"Dear Client,\n\n"
            f"Your transaction has been completed and is now ready for release.\n\n"
            f"Transaction Details:\n"
            f"• Tracking ID: {case.tracking_id}\n"
            f"• Current Status: {dict(Case.STATUS_CHOICES).get(case.status, case.status)}\n\n"
            f"Please prepare any required identification or supporting documents when claiming your records, if applicable.\n\n"
            f"You may review your transaction details through the PASTrack portal.\n\n"
            f"For office schedules, FAQs, and assistance, please contact the Provincial Assessor's Office.\n\n"
            f"Thank you for using PASTrack."
        ),
    )
    sns_hook(event="case_released", payload={"tracking_id": case.tracking_id, "status": case.status})

    

    messages.success(request, f"Case {case.tracking_id} marked as Released.")
    return redirect("case_detail", tracking_id=case.tracking_id)


@login_required
@require_POST
def upload_correction_document(request, tracking_id, doc_id):
    case = get_object_or_404(Case, tracking_id=tracking_id)
    doc = get_object_or_404(CaseDocument, id=doc_id, case=case)

    role = getattr(request.user, "role", "")
    if role not in ["capitol_receiving", "capitol_examiner"]:
        return JsonResponse({"error": "Unauthorized to upload corrections inline."}, status=403)
        
    if role == "capitol_receiving" and case.status not in {"client_correction", "not_received"}:
        return JsonResponse({"error": "Case is not in correction state for Receiver."}, status=400)
        
    if role == "capitol_examiner" and case.status not in {"in_review", "to_examine"}:
        return JsonResponse({"error": "Case is not in review state for Examiner."}, status=400)
        
    if "file" not in request.FILES:
        return JsonResponse({"error": "No file uploaded."}, status=400)
        
    new_file = request.FILES["file"]
    
    # Create DocumentVersion of the old file
    from .models import DocumentVersion
    DocumentVersion.objects.create(
        case=case,
        doc_type=doc.doc_type,
        file=doc.file,
        uploaded_by=doc.uploaded_by,
        uploaded_at=doc.uploaded_at
    )
    
    # Update the CaseDocument with the new file
    doc.file = new_file
    doc.uploaded_by = request.user
    doc.uploaded_at = timezone.now()
    doc.reviewed_ok = False
    doc.review_remark = ""
    doc.save()
    
    AuditLog.objects.create(
        actor=request.user,
        action="case_update",
        target_object=f"Case: {case.tracking_id}",
        details={"corrected_document": doc.doc_type}
    )
    
    return JsonResponse({
        "success": True, 
        "message": "File updated successfully.", 
        "uploaded_at": doc.uploaded_at.strftime("%b %d, %Y")
    })