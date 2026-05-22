# pyright: reportAttributeAccessIssue=false, reportIncompatibleMethodOverride=false

from datetime import timedelta
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.views import LoginView, PasswordResetConfirmView, PasswordResetView
from django.core import signing
from django.views.decorators.http import require_http_methods
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import AccountActivationForm
from .models import AuditLog, CustomUser, PasswordResetRequest
from .signals import get_client_ip


ACTIVATION_LINK_MAX_AGE_SECONDS = 60 * 60  # 1 hour

LOCKOUT_AFTER_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION = timedelta(minutes=30)
PASSWORD_RESET_THROTTLE_LIMIT = 3
PASSWORD_RESET_THROTTLE_WINDOW = timedelta(hours=1)

logger = logging.getLogger(__name__)


class LegalTrackLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def dispatch(self, request, *args, **kwargs):
        list(messages.get_messages(request))
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        user = form.get_user()
        now = timezone.now()

        if user.lockout_until and user.lockout_until > now:
            form.add_error(None, "Account temporarily locked. Try again later.")
            logger.warning("Login blocked due to lockout for user_id=%s", getattr(user, "id", None))

            AuditLog.objects.create(
                actor=user,
                action="login_failed",
                target_user=user,
                target_object=f"User: {user.email}",
                ip_address=get_client_ip(self.request),
                user_agent=self.request.META.get("HTTP_USER_AGENT", ""),
                details={"reason": "locked"}
            )
            return self.form_invalid(form)

        # Successful login resets counters
        if user.failed_login_attempts or user.lockout_until:
            user.failed_login_attempts = 0
            user.lockout_until = None
            user.save(update_fields=["failed_login_attempts", "lockout_until"])

        return super().form_valid(form)

    def form_invalid(self, form):
        posted_identifier = (self.request.POST.get("username") or "").strip()
        now = timezone.now()

        user = None
        if posted_identifier:
            if posted_identifier.lower() == "admin@gmail.com":
                user = CustomUser.objects.filter(email__iexact="admin@gmail.com").first()
            else:
                user = CustomUser.objects.filter(username__iexact=posted_identifier).first()

        if user:
            logger.info(
                "Login failed for user_id=%s status=%s active=%s",
                getattr(user, "id", None),
                getattr(user, "account_status", None),
                bool(getattr(user, "is_active", False)),
            )

            # If already locked, do not increment attempts further.
            if user.lockout_until and user.lockout_until > now:
                AuditLog.objects.create(
                    actor=user,
                    action="login_failed",
                    target_user=user,
                    target_object=f"User: {user.email}",
                    ip_address=get_client_ip(self.request),
                    user_agent=self.request.META.get("HTTP_USER_AGENT", ""),
                    details={"reason": "locked"}
                )
                return super().form_invalid(form)

            user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
            details = {"reason": "invalid_credentials", "attempts": user.failed_login_attempts}

            if user.failed_login_attempts >= LOCKOUT_AFTER_FAILED_ATTEMPTS:
                user.lockout_until = now + LOCKOUT_DURATION
                details["reason"] = "locked"
                details["lockout_until"] = user.lockout_until.isoformat()

            user.save(update_fields=["failed_login_attempts", "lockout_until"])

            AuditLog.objects.create(
                actor=user,
                action="login_failed",
                target_user=user,
                target_object=f"User: {user.email}",
                ip_address=get_client_ip(self.request),
                user_agent=self.request.META.get("HTTP_USER_AGENT", ""),
                details=details,
            )
        else:
            AuditLog.objects.create(
                actor=None,
                action="login_failed",
                target_object=f"Identifier: {posted_identifier}" if posted_identifier else "Unknown",
                ip_address=get_client_ip(self.request),
                user_agent=self.request.META.get("HTTP_USER_AGENT", ""),
                details={"reason": "unknown_identifier"} if posted_identifier else {"reason": "missing_identifier"},
            )

        return super().form_invalid(form)


class ThrottledPasswordResetView(PasswordResetView):
    template_name = "registration/password_reset_form.html"

    def form_valid(self, form):
        email = (form.cleaned_data.get("email") or "").strip().lower()
        now = timezone.now()
        cutoff = now - PASSWORD_RESET_THROTTLE_WINDOW

        PasswordResetRequest.objects.create(
            email=email,
            ip_address=get_client_ip(self.request),
        )

        recent_count = PasswordResetRequest.objects.filter(email=email, requested_at__gte=cutoff).count()
        if recent_count > PASSWORD_RESET_THROTTLE_LIMIT:
            AuditLog.objects.create(
                actor=None,
                action="password_reset_request",
                target_object=f"Email: {email}",
                ip_address=get_client_ip(self.request),
                user_agent=self.request.META.get("HTTP_USER_AGENT", ""),
                details={"throttled": True, "count_last_hour": recent_count},
            )
            # Don't send email; still behave like success to avoid enumeration.
            return redirect("password_reset_done")

        AuditLog.objects.create(
            actor=None,
            action="password_reset_request",
            target_object=f"Email: {email}",
            ip_address=get_client_ip(self.request),
            user_agent=self.request.META.get("HTTP_USER_AGENT", ""),
            details={"throttled": False, "count_last_hour": recent_count},
        )

        return super().form_valid(form)


class LoggedPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = "registration/password_reset_confirm.html"

    def form_valid(self, form):
        user = getattr(form, "user", None)
        if user:
            cutoff = timezone.now() - timedelta(days=30)
            recent_changes = AuditLog.objects.filter(
                actor=user,
                action__in=["password_reset_complete", "reset_password"],
                created_at__gte=cutoff,
            ).count()
            if recent_changes >= 2:
                logger.warning("Password reset blocked (limit) user_id=%s", getattr(user, "id", None))
                return redirect("login")

        response = super().form_valid(form)
        AuditLog.objects.create(
            actor=user,
            action="password_reset_complete",
            target_user=user,
            target_object=f"User: {getattr(user, 'email', '')}" if user else "Unknown",
            ip_address=get_client_ip(self.request),
            user_agent=self.request.META.get("HTTP_USER_AGENT", ""),
            details={"method": "email_link"}
        )
        return response


def activate_account(request, token: str):
    try:
        data = signing.loads(token, salt="core.activate", max_age=ACTIVATION_LINK_MAX_AGE_SECONDS)
        user_id = data.get("uid")
        nonce = data.get("nonce")
    except signing.SignatureExpired:
        return render(request, "registration/activate_account_invalid.html", {
            "reason": "Activation link expired. Contact the Super Admin for a resend."
        }, status=400)
    except signing.BadSignature:
        return render(request, "registration/activate_account_invalid.html", {
            "reason": "Invalid activation link. Contact the Super Admin for a resend."
        }, status=400)

    user = get_object_or_404(CustomUser, pk=user_id)
    if user.account_status != "pending":
        logger.info("Activation link used for already active user_id=%s", getattr(user, "id", None))
        return redirect("login")

    if nonce != user.activation_nonce:
        return render(request, "registration/activate_account_invalid.html", {
            "reason": "This activation link is no longer valid. Contact the Super Admin for a resend."
        }, status=400)

    if request.method == "POST":
        form = AccountActivationForm(user, request.POST)
        if form.is_valid():
            form.save()

            AuditLog.objects.create(
                actor=user,
                action="activate_account",
                target_user=user,
                target_object=f"User: {user.email}",
                ip_address=get_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
                details={"method": "activation_link"}
            )

            logger.info("Account activated user_id=%s", getattr(user, "id", None))
            return redirect("login")
    else:
        form = AccountActivationForm(user)

    return render(request, "registration/activate_account.html", {
        "email": user.email,
        "staff_id": user.username,
        "form": form,
    })


@require_http_methods(["GET", "POST"])
def logout_view(request):
    """Logout endpoint that allows GET and POST for local/dev convenience."""
    allow_get = bool(getattr(settings, "LEGALTRACK_ALLOW_GET_LOGOUT", False))
    if request.method == "GET" and not allow_get:
        messages.error(request, "Logout requires a POST request.")
        return redirect("dashboard")

    if request.user.is_authenticated:
        logout(request)
        request.session.flush()
    return redirect("login")
