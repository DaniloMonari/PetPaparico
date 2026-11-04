import time as time_module
from datetime import date, datetime, time, timedelta

from django.contrib import messages
from django.contrib.auth import authenticate, login as django_login, logout as django_logout
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .iot_client import esp32_client
from decimal import Decimal
from .models import (
    CameraFeed,
    CustomUser,
    FeedingHistory,
    FeedingSchedule,
    MediaCapture,
    Notification,
    NotificationSettings,
    Pet,
)
from .notification_utils import (
    check_and_notify_dispenser_cleaning,
    check_and_notify_low_food,
    create_notification,
    notify_feeding_completed,
)
from .servo_views import calculate_duration
from .web_helpers import (
    clean_camera_url,
    embed_camera_url,
    feeding_stats,
    get_selected_pet,
    get_user_pets,
    history_stats,
    nav_context,
    sync_device_status,
    user_display_name,
    user_photo_url,
)


def dashboard_stats(user):
    today = timezone.localdate()
    feedings_today = FeedingHistory.objects.filter(
        pet__user=user, timestamp__date=today, success=True
    ).count()

    settings, _ = NotificationSettings.objects.get_or_create(user=user)
    reminder_days = settings.dispenserCleaningReminderDays if settings.dispenserCleaningReminderDays in (1, 2, 3, 4) else 3
    last_cleaning = settings.lastDispenserCleaningReminderAt
    cleaning_due = True
    days_until_cleaning = 0

    if last_cleaning is not None:
        elapsed = timezone.now() - last_cleaning
        cleaning_due = elapsed >= timedelta(days=reminder_days)
        days_until_cleaning = max(0, reminder_days - elapsed.days)
    else:
        cleaning_due = True
        days_until_cleaning = reminder_days

    # Obter status de todos os canais
    devices, _ = sync_device_status(user)

    # Calcular nível médio de ração (se houver dispositivos)
    total_food = 0
    online_count = 0
    food_levels = []

    if isinstance(devices, list):
        for device in devices:
            if device.foodLevel is not None:
                total_food += device.foodLevel
                food_levels.append(device.foodLevel)
            if device.isOnline:
                online_count += 1
    elif devices and devices.foodLevel is not None:
        total_food = devices.foodLevel
        food_levels = [devices.foodLevel]
        if devices.isOnline:
            online_count = 1

    avg_food_level = total_food / len(food_levels) if food_levels else None

    return {
        "pets_count": Pet.objects.filter(user=user).count(),
        "feedings_today": feedings_today,
        "food_level": avg_food_level,
        "device_online": online_count > 0,
        "online_devices_count": online_count,
        "total_devices_count": 5,  # Sempre 5 canais disponíveis
        "dispenser_cleaning_due": cleaning_due,
        "dispenser_cleaning_days": days_until_cleaning,
    }


# --- Auth & home ---


@require_http_methods(["GET"])
def home(request):
    if request.user.is_authenticated:
        current_user = CustomUser.objects.get(pk=request.user.pk)
        pets = Pet.objects.filter(user=current_user).order_by("-createdAt")
        return render(
            request,
            "api/home_dashboard.html",
            {
                **nav_context(request),
                "pets": pets,
                **dashboard_stats(current_user),
            },
        )
    return render(request, "api/home_landing.html", {"show_nav": False})


@require_http_methods(["GET", "POST"])
def login_page(request):
    if request.user.is_authenticated:
        return redirect("web_home")
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        if not username or not password:
            messages.error(request, "Preencha username e senha.")
        else:
            user = authenticate(request, username=username, password=password)
            if user:
                django_login(request, user)
                messages.success(request, "Login realizado com sucesso!")
                return redirect(request.GET.get("next") or reverse("web_home"))
            messages.error(request, "Credenciais inválidas.")
        return render(request, "api/login.html", {"show_nav": False, "username": username})
    return render(request, "api/login.html", {"show_nav": False})


@require_http_methods(["GET", "POST"])
def register_page(request):
    if request.user.is_authenticated:
        return redirect("web_home")
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        email = request.POST.get("email", "").strip()
        password = request.POST.get("password", "")
        confirm = request.POST.get("confirm_password", "")
        cpf = request.POST.get("cpf", "").strip()
        telefone = request.POST.get("telefone", "").strip()
        profissao = request.POST.get("profissao", "").strip()
        cidade = request.POST.get("cidade", "").strip()
        estado = request.POST.get("estado", "").strip().upper()

        if not username or not password:
            messages.error(request, "Username e senha são obrigatórios.")
        elif password != confirm:
            messages.error(request, "As senhas não coincidem.")
        elif len(password) < 6:
            messages.error(request, "A senha deve ter pelo menos 6 caracteres.")
        elif CustomUser.objects.filter(username=username).exists():
            messages.error(request, "Este username já está em uso.")
        else:
            user = CustomUser.objects.create_user(
                username=username,
                email=email,
                password=password,
                is_active=True,
                cpf=cpf or None,
                telefone=telefone or None,
                profissao=profissao or None,
                cidade=cidade or None,
                estado=estado or None,
            )
            django_login(request, user)
            messages.success(request, "Conta criada com sucesso!")
            return redirect("web_home")
        return render(
            request,
            "api/register.html",
            {
                "show_nav": False,
                "username": username,
                "email": email,
                "cpf": cpf,
                "telefone": telefone,
                "profissao": profissao,
                "cidade": cidade,
                "estado": estado,
            },
        )
    return render(request, "api/register.html", {"show_nav": False})


@require_POST
@login_required(login_url="/login/")
def logout_page(request):
    django_logout(request)
    messages.success(request, "Logout realizado.")
    return redirect("web_home")


# --- Pets ---


@login_required(login_url="/login/")
def pets_list(request):
    return render(
        request,
        "api/pets_list.html",
        {**nav_context(request), "pets": get_user_pets(request.user)},
    )


@login_required(login_url="/login/")
def schedule_events_page(request):
    queryset = FeedingHistory.objects.filter(pet__user=request.user).select_related("pet")

    pet_id = request.GET.get("pet")
    status = request.GET.get("status")
    period = request.GET.get("period")

    if pet_id:
        queryset = queryset.filter(pet_id=pet_id)
    if status in {"success", "failure"}:
        queryset = queryset.filter(success=(status == "success"))

    if period == "7d":
        cutoff = timezone.now() - timedelta(days=7)
        queryset = queryset.filter(timestamp__gte=cutoff)
    elif period == "30d":
        cutoff = timezone.now() - timedelta(days=30)
        queryset = queryset.filter(timestamp__gte=cutoff)
    elif period == "90d":
        cutoff = timezone.now() - timedelta(days=90)
        queryset = queryset.filter(timestamp__gte=cutoff)

    events = queryset.order_by("-timestamp")[:100]

    return render(
        request,
        "api/schedule_events.html",
        {
            **nav_context(request),
            "events": events,
            "pets": get_user_pets(request.user),
            "selected_pet_id": pet_id,
            "selected_status": status,
            "selected_period": period,
        },
    )


@login_required(login_url="/login/")
@require_http_methods(["GET", "POST"])
def pet_create(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        if not name:
            messages.error(request, "O nome do pet é obrigatório.")
        else:
            age = request.POST.get("age") or None
            weight_raw = request.POST.get("weight") or None
            channel_raw = request.POST.get("dispenser_channel") or None
            weight = None
            dispenser_channel = None
            if weight_raw:
                try:
                    weight = Decimal(weight_raw.replace(',', '.'))
                except Exception:
                    messages.error(request, "Peso inválido.")
                    return render(request, "api/pet_form.html", {**nav_context(request), "pet": None})

            if channel_raw:
                try:
                    dispenser_channel = int(channel_raw)
                except Exception:
                    messages.error(request, "Canal do dispensador inválido.")
                    return render(request, "api/pet_form.html", {**nav_context(request), "pet": None})

            if dispenser_channel and Pet.objects.filter(user=request.user, dispenser_channel=dispenser_channel).exists():
                messages.error(request, f"O reservatório {dispenser_channel} já está atribuído a outro pet.")
                return render(request, "api/pet_form.html", {**nav_context(request), "pet": None})

            Pet.objects.create(
                user=request.user,
                name=name,
                species=request.POST.get("species") or None,
                breed=request.POST.get("breed") or None,
                age=int(age) if age else None,
                weight=weight,
                dispenser_channel=dispenser_channel,
                notes=request.POST.get("notes") or None,
                photo=request.FILES.get("photo"),
            )
            messages.success(request, "Pet cadastrado!")
            return redirect("web_pets")
    return render(request, "api/pet_form.html", {**nav_context(request), "pet": None})


@login_required(login_url="/login/")
@require_http_methods(["GET", "POST"])
def pet_edit(request, pk):
    pet = get_object_or_404(Pet, pk=pk, user=request.user)
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        if not name:
            messages.error(request, "O nome do pet é obrigatório.")
        else:
            pet.name = name
            pet.species = request.POST.get("species") or None
            pet.breed = request.POST.get("breed") or None
            age, weight_raw = request.POST.get("age"), request.POST.get("weight")
            channel_raw = request.POST.get("dispenser_channel") or None
            pet.age = int(age) if age else None
            if weight_raw:
                try:
                    pet.weight = Decimal(weight_raw.replace(',', '.'))
                except Exception:
                    messages.error(request, "Peso inválido.")
                    return render(request, "api/pet_form.html", {**nav_context(request), "pet": pet})
            else:
                pet.weight = None

            if channel_raw:
                try:
                    dispenser_channel = int(channel_raw)
                except Exception:
                    messages.error(request, "Canal do dispensador inválido.")
                    return render(request, "api/pet_form.html", {**nav_context(request), "pet": pet})
                if Pet.objects.filter(user=request.user, dispenser_channel=dispenser_channel).exclude(pk=pet.pk).exists():
                    messages.error(request, f"O reservatório {dispenser_channel} já está atribuído a outro pet.")
                    return render(request, "api/pet_form.html", {**nav_context(request), "pet": pet})
                pet.dispenser_channel = dispenser_channel
            else:
                pet.dispenser_channel = None

            pet.notes = request.POST.get("notes") or None
            if request.FILES.get("photo"):
                pet.photo = request.FILES["photo"]
            pet.save()
            messages.success(request, "Pet atualizado!")
            return redirect("web_pets")
    return render(request, "api/pet_form.html", {**nav_context(request), "pet": pet})


@login_required(login_url="/login/")
@require_POST
def pet_delete(request, pk):
    pet = get_object_or_404(Pet, pk=pk, user=request.user)
    pet.delete()
    messages.success(request, "Pet removido.")
    return redirect("web_pets")


@login_required(login_url="/login/")
@require_POST
def manual_feed(request, pk):
    pet = get_object_or_404(Pet, pk=pk, user=request.user)
    quantity = int(request.POST.get("quantity", 50))
    FeedingHistory.objects.create(
        pet=pet, quantity=quantity, feedingType="manual", success=True
    )
    # Enviar notificações
    try:
        notify_feeding_completed(user=pet.user, pet_name=pet.name, quantity=quantity)
    except Exception as e:
        print(f"Erro ao enviar notificação: {e}")
    messages.success(request, f"{pet.name} alimentado com {quantity}g!")
    return redirect(request.POST.get("next") or reverse("web_home"))


# --- Feeding ---


@login_required(login_url="/login/")
@require_http_methods(["GET", "POST"])
def feeding_page(request):
    pets = get_user_pets(request.user)
    selected_pet = get_selected_pet(request.user, request.GET.get("pet") or request.POST.get("pet_id"))
    
    # Obter canal do pet selecionado (se houver)
    channel = selected_pet.dispenser_channel if selected_pet else None
    
    # Sincronizar status do canal específico
    device, esp32 = sync_device_status(request.user, channel)
    
    # Obter nível de ração do canal específico
    if esp32:
        food_level = esp32.get("food_level", 85)
        is_online = esp32.get("online", True)
    elif device:
        food_level = device.foodLevel if device.foodLevel is not None else 85
        is_online = device.isOnline
    else:
        food_level = 85
        is_online = True

    if request.method == "POST":
        action = request.POST.get("action")
        pet = get_selected_pet(request.user, request.POST.get("pet_id"))
        if not pet:
            messages.error(request, "Selecione um pet.")
            return redirect("web_feeding")

        if action == "manual_feed":
            qty = int(request.POST.get("quantity", 50))
            FeedingHistory.objects.create(
                pet=pet, quantity=qty, feedingType="manual", success=True
            )
            # Enviar notificações
            try:
                notify_feeding_completed(user=pet.user, pet_name=pet.name, quantity=qty)
                # Verificar se ração está baixa (simulando food_level após dispensação)
                device, _ = sync_device_status(pet.user)
                if device and device.foodLevel is not None:
                    check_and_notify_low_food(pet.user, device.foodLevel)
            except Exception as e:
                print(f"Erro ao enviar notificação: {e}")
            messages.success(request, f"Alimentação de {qty}g registrada para {pet.name}!")
        elif action == "save_quantity":
            qty = int(request.POST.get("quantity", pet.defaultQuantity))
            pet.defaultQuantity = qty
            pet.save()
            messages.success(request, f"Porção padrão de {qty}g salva para {pet.name}!")
        elif action == "schedule":
            FeedingSchedule.objects.create(
                pet=pet,
                time=request.POST.get("time", "08:00"),
                quantity=int(request.POST.get("schedule_quantity", 50)),
                frequency=request.POST.get("frequency", "daily"),
                isActive=True,
            )
            messages.success(request, "Agendamento criado!")
        elif action == "edit_schedule":
            schedule_id = request.POST.get("schedule_id")
            schedule = FeedingSchedule.objects.filter(id=schedule_id, pet=pet).first()
            if not schedule:
                messages.error(request, "Agendamento não encontrado.")
            else:
                schedule.time = request.POST.get("time", schedule.time)
                schedule.quantity = int(request.POST.get("schedule_quantity", schedule.quantity))
                schedule.frequency = request.POST.get("frequency", schedule.frequency)
                schedule.save()
                messages.success(request, "Agendamento atualizado!")
        elif action == "delete_schedule":
            schedule_id = request.POST.get("schedule_id")
            schedule = FeedingSchedule.objects.filter(id=schedule_id, pet=pet).first()
            if not schedule:
                messages.error(request, "Agendamento não encontrado.")
            else:
                schedule.delete()
                messages.success(request, "Agendamento removido!")
        elif action == "refill":
            esp32_client.update_simulator({"food_level": 100})
            sync_device_status(request.user)
            messages.success(request, "Reservatório marcado como cheio (100%).")

        return redirect(f"{reverse('web_feeding')}?pet={pet.id}")

    ctx = {
        **nav_context(request),
        "pets": pets,
        "selected_pet": selected_pet,
        "food_level": food_level or 0,
        "is_online": is_online,
        "filter_period": request.GET.get("filter", "all"),
    }
    if selected_pet:
        ctx.update(feeding_stats(selected_pet, request.GET.get("filter", "all")))
    return render(request, "api/feeding.html", ctx)


# --- History ---


@login_required(login_url="/login/")
@require_http_methods(["GET", "POST"])
def history_page(request):
    pets = get_user_pets(request.user)
    selected_pet = get_selected_pet(
        request.user,
        request.GET.get("pet") or request.POST.get("pet_id"),
    )
    filter_period = request.GET.get("filter", request.POST.get("filter", "week"))
    if filter_period not in {"day", "week", "month", "year", "all"}:
        filter_period = "week"

    if request.method == "POST" and request.POST.get("action") == "delete_feeding":
        feeding = get_object_or_404(
            FeedingHistory,
            pk=request.POST.get("feeding_id"),
            pet=selected_pet,
            pet__user=request.user,
        )
        feeding.delete()
        messages.success(request, "Registro de alimentação excluído.")
        return redirect(
            f"{reverse('web_history')}?pet={selected_pet.id}&filter={filter_period}"
        )

    ctx = {
        **nav_context(request),
        "pets": pets,
        "selected_pet": selected_pet,
        "filter_period": filter_period,
    }
    if selected_pet:
        history_queryset = FeedingHistory.objects.filter(pet=selected_pet).order_by("-timestamp")
        paginator = Paginator(history_queryset, 6)
        page_number = request.GET.get("page")
        history_page = paginator.get_page(page_number)
        
        history_records = list(history_queryset)
        ctx["history"] = history_page
        ctx["paginator"] = paginator
        ctx.update(history_stats(history_records))

        dashboard_stats = feeding_stats(selected_pet, filter_period)
        today = timezone.localdate()
        weekday_labels = ("Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom")

        if filter_period == "month":
            month_start = today.replace(day=1)
            next_month = (month_start + timedelta(days=32)).replace(day=1)
            days_in_month = (next_month - month_start).days
            week_count = (days_in_month + 6) // 7
            feedings_by_day_percent = [
                {
                    "label": f"{week * 7 + 1:02d}–{min(week * 7 + 7, days_in_month):02d}",
                    "amount": 0,
                }
                for week in range(week_count)
            ]
            for feeding in history_queryset:
                feeding_date = timezone.localtime(feeding.timestamp).date()
                if feeding_date.year == today.year and feeding_date.month == today.month:
                    bucket = (feeding_date.day - 1) // 7
                    feedings_by_day_percent[bucket]["amount"] += feeding.quantity
            chart_title = "Consumo por semana"
            chart_description = "Mês atual · dias do mês"
        elif filter_period == "day":
            feedings_by_day_percent = [
                {"label": f"{hour:02d}h", "amount": 0}
                for hour in range(0, 24, 2)
            ]
            for feeding in history_queryset:
                local_timestamp = timezone.localtime(feeding.timestamp)
                if local_timestamp.date() == today:
                    bucket = local_timestamp.hour // 2
                    feedings_by_day_percent[bucket]["amount"] += feeding.quantity
            chart_title = "Consumo por horário"
            chart_description = "Hoje · faixas de duas horas"
        elif filter_period == "week":
            week_start = today - timedelta(days=today.weekday())
            feedings_by_day_percent = [
                {
                    "label": f"{weekday_labels[offset]} {(week_start + timedelta(days=offset)).day:02d}",
                    "amount": 0,
                }
                for offset in range(7)
            ]
            for feeding in history_queryset:
                feeding_date = timezone.localtime(feeding.timestamp).date()
                offset = (feeding_date - week_start).days
                if 0 <= offset < 7:
                    feedings_by_day_percent[offset]["amount"] += feeding.quantity
            chart_title = "Consumo por dia"
            chart_description = "Semana atual · segunda a domingo"
        elif filter_period == "year":
            month_labels = ("Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez")
            feedings_by_day_percent = [
                {"label": month, "amount": 0} for month in month_labels
            ]
            for feeding in history_queryset:
                local_timestamp = timezone.localtime(feeding.timestamp)
                if local_timestamp.year == today.year:
                    feedings_by_day_percent[local_timestamp.month - 1]["amount"] += feeding.quantity
            chart_title = "Consumo por mês"
            chart_description = f"{today.year} · evolução mensal"
        else:
            feedings_by_day_percent = [
                {"label": day, "amount": 0} for day in weekday_labels
            ]
            for feeding in history_queryset:
                weekday = timezone.localtime(feeding.timestamp).weekday()
                feedings_by_day_percent[weekday]["amount"] += feeding.quantity
            chart_title = "Ritmo por dia da semana"
            chart_description = "Todo o histórico · padrão de consumo"

        max_day_total = max(item["amount"] for item in feedings_by_day_percent) if feedings_by_day_percent else 1
        period_color = {
            "day": {"start": "#2563eb", "end": "#38bdf8"},
            "week": {"start": "#7c3aed", "end": "#d946ef"},
            "month": {"start": "#16a34a", "end": "#84cc16"},
            "year": {"start": "#d97706", "end": "#f59e0b"},
            "all": {"start": "#4f46e5", "end": "#818cf8"},
        }
        selected_colors = period_color.get(filter_period, period_color["all"])
        for item in feedings_by_day_percent:
            amount = item["amount"]
            item["percent"] = min(100, int(amount * 100 / max_day_total)) if max_day_total else 0
            item["height_pct"] = max(8, min(100, int(amount * 100 / max_day_total))) if amount and max_day_total else 0
            item["bar_color_start"] = selected_colors["start"]
            item["bar_color_end"] = selected_colors["end"]

        local_now = timezone.localtime()
        period_start = None
        if filter_period == "day":
            period_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif filter_period == "week":
            period_start = (local_now - timedelta(days=local_now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        elif filter_period == "month":
            period_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif filter_period == "year":
            period_start = local_now.replace(
                month=1, day=1, hour=0, minute=0, second=0, microsecond=0
            )

        feedings_by_hour = [0] * 24
        feeding_type_counts = {"manual": 0, "scheduled": 0}
        for feeding in history_records:
            local_timestamp = timezone.localtime(feeding.timestamp)
            if period_start is not None and local_timestamp < period_start:
                continue
            feedings_by_hour[local_timestamp.hour] += feeding.quantity
            if feeding.feedingType in feeding_type_counts:
                feeding_type_counts[feeding.feedingType] += 1

        type_count_total = sum(feeding_type_counts.values())
        manual_percent = round(feeding_type_counts["manual"] * 100 / type_count_total) if type_count_total else 0
        scheduled_percent = 100 - manual_percent if type_count_total else 0

        period_names = {
            "day": "Hoje",
            "week": "Semana",
            "month": "Mês",
            "year": "Ano",
            "all": "Todos",
        }

        ctx.update({
            "period_name": period_names.get(filter_period, "Semana"),
            "period_total": int(dashboard_stats.get("total_kg_period", 0) * 1000),
            "period_count": dashboard_stats.get("count_period", 0),
            "period_avg": dashboard_stats.get("avg_period", 0),
            "total_kg_all": dashboard_stats.get("total_kg_all", 0),
            "last_feeding": dashboard_stats.get("last_feeding"),
            "next_schedule_time": dashboard_stats.get("next_schedule_time", "---"),
            "feedings_by_day_percent": feedings_by_day_percent,
            "feedings_by_hour": feedings_by_hour,
            "feeding_type_counts": feeding_type_counts,
            "feeding_type_total": type_count_total,
            "manual_percent": manual_percent,
            "scheduled_percent": scheduled_percent,
            "max_day_total": max_day_total,
            "chart_title": chart_title,
            "chart_description": chart_description,
        })
    return render(request, "api/history.html", ctx)


# --- Cameras ---


@login_required(login_url="/login/")
@require_http_methods(["GET", "POST"])
def cameras_page(request):
    pets = get_user_pets(request.user)
    selected_pet = get_selected_pet(request.user, request.GET.get("pet") or request.POST.get("pet_id"))

    if request.method == "POST":
        action = request.POST.get("action")
        pet = get_selected_pet(request.user, request.POST.get("pet_id"))
        if action == "add_camera" and pet:
            name = request.POST.get("name", "").strip()
            url = request.POST.get("rtspUrl", "").strip()
            if name and url:
                cleaned_url = clean_camera_url(url)
                if cleaned_url:
                    CameraFeed.objects.create(
                        pet=pet, name=name, rtspUrl=cleaned_url, isActive=True
                    )
                    messages.success(request, "Câmera adicionada!")
                else:
                    messages.error(request, "URL da câmera inválida ou não suportada.")
            else:
                messages.error(request, "Preencha nome e URL.")
            return redirect(f"{reverse('web_cameras')}?pet={pet.id}")
        if action == "edit_camera":
            cam = get_object_or_404(CameraFeed, pk=request.POST.get("camera_id"), pet__user=request.user)
            name = request.POST.get("name", "").strip()
            url = request.POST.get("rtspUrl", "").strip()
            if name and url:
                cleaned_url = clean_camera_url(url)
                if cleaned_url:
                    cam.name = name
                    cam.rtspUrl = cleaned_url
                    cam.save()
                    messages.success(request, "Câmera atualizada!")
                else:
                    messages.error(request, "URL da câmera inválida ou não suportada.")
            else:
                messages.error(request, "Preencha nome e URL.")
            return redirect(f"{reverse('web_cameras')}?pet={cam.pet_id}")
        if action == "delete_camera":
            cam = get_object_or_404(CameraFeed, pk=request.POST.get("camera_id"), pet__user=request.user)
            pid = cam.pet_id
            cam.delete()
            messages.success(request, "Câmera removida.")
            return redirect(f"{reverse('web_cameras')}?pet={pid}")
        if action == "delete_capture":
            cap = get_object_or_404(MediaCapture, pk=request.POST.get("capture_id"), cameraFeed__pet__user=request.user)
            pid = cap.cameraFeed.pet_id
            cap.delete()
            messages.success(request, "Captura excluída.")
            return redirect(f"{reverse('web_cameras')}?pet={pid}")

    cameras = []
    captures = []
    if selected_pet:
        cameras = CameraFeed.objects.filter(pet=selected_pet)
        captures = MediaCapture.objects.filter(cameraFeed__pet=selected_pet).order_by("-timestamp")[:24]
        for cam in cameras:
            cam.embed_url = embed_camera_url(cam.rtspUrl)

    return render(
        request,
        "api/cameras.html",
        {
            **nav_context(request),
            "pets": pets,
            "selected_pet": selected_pet,
            "cameras": cameras,
            "captures": captures,
        },
    )


# --- Device ---


@login_required(login_url="/login/")
@require_http_methods(["GET", "POST"])
def device_page(request):
    pets = get_user_pets(request.user)
    selected_pet = get_selected_pet(request.user, request.GET.get("pet") or request.POST.get("pet_id"))
    
    # Obter canal do pet selecionado (se houver)
    channel = selected_pet.dispenser_channel if selected_pet else None
    
    # Sincronizar status do canal específico ou todos se nenhum pet selecionado
    device, esp32 = sync_device_status(request.user, channel)
    
    simulator = {
        "simulator_enabled": esp32_client.simulator_enabled,
        "state": esp32_client.simulated_states.get(channel or 0, {}),
    }

    if request.method == "POST":
        action = request.POST.get("action")
        channel_raw = request.POST.get("channel")
        channel = int(channel_raw) if channel_raw else None
        pet = None
        if channel:
            pet = Pet.objects.filter(user=request.user, dispenser_channel=channel).first()
        if action == "feed" and not channel and selected_pet:
            pet = selected_pet
            channel = selected_pet.dispenser_channel
        if action == "feed" and channel and pet:
            qty = int(request.POST.get("quantity", pet.defaultQuantity))
            if esp32_client.is_online():
                result = esp32_client.dispense_food(
                    quantity=qty,
                    duration_ms=calculate_duration(qty),
                    feeding_id=f"web_{pet.id}_{int(time_module.time())}",
                    channel=channel,
                )
                if result.get("success"):
                    FeedingHistory.objects.create(
                        pet=pet, quantity=qty, feedingType="manual", success=True
                    )
                    # Enviar notificações
                    try:
                        notify_feeding_completed(user=pet.user, pet_name=pet.name, quantity=qty)
                        food_level_after = result.get("food_level_after", 85)
                        check_and_notify_low_food(pet.user, food_level_after)
                    except Exception as e:
                        print(f"Erro ao enviar notificação: {e}")
                    messages.success(request, f"{qty}g dispensados para {pet.name} no reservatório {channel}!")
                else:
                    messages.error(request, result.get("error", "Falha no dispensador."))
            else:
                FeedingHistory.objects.create(
                    pet=pet, quantity=qty, feedingType="manual", success=False,
                    notes="ESP32 offline",
                )
                messages.warning(request, "ESP32 offline — registro salvo sem dispensar.")
        elif action == "feed" and channel and not pet:
            messages.error(request, f"Nenhum pet atribuído ao reservatório {channel}.")
        elif action == "calibrate":
            pos = max(0, min(180, int(request.POST.get("position", 0))))
            if esp32_client.is_online():
                esp32_client.calibrate_servo(pos)
                messages.success(request, f"Servo calibrado para {pos}°.")
            else:
                messages.error(request, "ESP32 offline.")
        elif action == "test":
            if esp32_client.is_online():
                messages.success(request, "Conexão com ESP32 OK!")
            else:
                messages.error(request, "ESP32 não responde.")
        elif action == "refill":
            esp32_client.update_simulator({"food_level": 100})
            messages.success(request, "Reservatório em 100%.")
        sync_device_status(request.user)
        return redirect("web_device")

    status = {}
    if esp32:
        status = {
            "online": esp32_client.is_online(),
            "food_level": esp32_client.simulated_state.get("food_level", 0),
            "battery_level": esp32_client.simulated_state.get("battery_level", 0),
            "is_dispensing": esp32_client.simulated_state.get("is_dispensing", False),
            "wifi_rssi": esp32_client.simulated_state.get("wifi_rssi", 0),
            "ip": esp32_client.simulated_state.get("ip", ""),
        }

    dispensers = []
    for channel in range(1, 6):
        pet_assigned = Pet.objects.filter(user=request.user, dispenser_channel=channel).first()
        dispensers.append({
            "channel": channel,
            "pet": pet_assigned,
            "quantity": pet_assigned.defaultQuantity if pet_assigned else 50,
        })

    return render(
        request,
        "api/device.html",
        {
            **nav_context(request),
            "pets": pets,
            "selected_pet": selected_pet,
            "status": status,
            "simulator": simulator,
            "dispensers": dispensers,
        },
    )


@login_required(login_url="/login/")
@require_http_methods(["GET", "POST"])
def calibrate_servo_page(request):
    """Página para calibrar o servo motor"""
    device, esp32 = sync_device_status(request.user)
    is_online = esp32.get("online", False) if esp32 else (device.isOnline if device else False)
    
    if request.method == "POST":
        position = max(0, min(90, int(request.POST.get("position", 0))))
        if esp32_client.is_online():
            result = esp32_client.calibrate_servo(position)
            if result.get("success"):
                messages.success(request, f"Servo calibrado para {position}° com sucesso!")
            else:
                messages.error(request, result.get("error", "Falha ao calibrar servo."))
        else:
            messages.error(request, "ESP32 offline. Não foi possível calibrar.")
        return redirect("web_calibrate_servo")
    
    return render(
        request,
        "api/calibrate_servo.html",
        {
            **nav_context(request),
            "is_online": is_online,
        },
    )


@login_required(login_url="/login/")
def test_connection_page(request):
    """Página para testar conexão com ESP32"""
    device, esp32 = sync_device_status(request.user)
    
    # Realizar teste de conexão
    is_online = esp32_client.is_online()
    device_info = None
    error_msg = None
    
    if is_online:
        try:
            device_info = esp32_client._request("GET", "/")
        except Exception as e:
            error_msg = str(e)
    
    status_data = {
        "online": is_online,
        "esp32_url": esp32_client.base_url,
        "device_info": device_info,
        "error": error_msg,
    }
    
    return render(
        request,
        "api/test_connection.html",
        {
            **nav_context(request),
            "status": status_data,
        },
    )


# --- Notifications ---


@login_required(login_url="/login/")
@require_http_methods(["GET", "POST"])
def notifications_page(request):
    settings_obj, _ = NotificationSettings.objects.get_or_create(user=request.user)
    notifications_list = Notification.objects.filter(user=request.user).order_by("-sentAt")

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save_settings":
            for field in (
                "emailFeedingCompleted", "emailLowFood", "emailDeviceOffline", "emailDispenserCleaning",
                "pushFeedingCompleted", "pushLowFood", "pushDeviceOffline", "pushDispenserCleaning",
            ):
                setattr(settings_obj, field, field in request.POST)
            settings_obj.lowFoodThreshold = int(request.POST.get("lowFoodThreshold", 20))
            settings_obj.dispenserCleaningReminderDays = int(request.POST.get("dispenserCleaningReminderDays", 3))
            settings_obj.save()
            messages.success(request, "Configurações salvas!")
        elif action == "mark_read":
            n = get_object_or_404(Notification, pk=request.POST.get("notification_id"), user=request.user)
            n.isRead = True
            n.readAt = timezone.now()
            n.save()
            messages.success(request, "Marcada como lida.")
        elif action == "mark_all_read":
            Notification.objects.filter(user=request.user, isRead=False).update(
                isRead=True, readAt=timezone.now()
            )
            messages.success(request, "Todas notificações marcadas como lidas!")
        elif action == "delete_notification":
            n = get_object_or_404(Notification, pk=request.POST.get("notification_id"), user=request.user)
            n.delete()
            messages.success(request, "Notificação excluída.")
        elif action == "delete_notifications_range":
            start_value = request.POST.get("start_date", "")
            end_value = request.POST.get("end_date", "")
            try:
                start_date = date.fromisoformat(start_value)
                end_date = date.fromisoformat(end_value)
            except ValueError:
                messages.error(request, "Informe datas válidas para excluir as notificações.")
                return redirect("web_notifications")

            if end_date < start_date:
                messages.error(request, "A data final deve ser igual ou posterior à data inicial.")
                return redirect("web_notifications")

            current_timezone = timezone.get_current_timezone()
            range_start = timezone.make_aware(
                datetime.combine(start_date, time.min),
                current_timezone,
            )
            range_end = timezone.make_aware(
                datetime.combine(end_date + timedelta(days=1), time.min),
                current_timezone,
            )
            deleted_count, _ = Notification.objects.filter(
                user=request.user,
                sentAt__gte=range_start,
                sentAt__lt=range_end,
            ).delete()
            messages.success(
                request,
                f"{deleted_count} notificação(ões) excluída(s) no período selecionado.",
            )
        elif action == "test_feeding":
            create_notification(
                request.user, "feeding_completed",
                "✅ Teste - Alimentação concluída",
                "Notificação de teste de alimentação.",
            )
            messages.success(request, "Teste de alimentação enviado!")
        elif action == "test_low_food":
            check_and_notify_low_food(request.user, 15)
            messages.success(request, "Teste de ração baixa enviado!")
        elif action == "test_offline":
            create_notification(
                request.user, "device_offline",
                "🔴 Teste - Dispositivo offline",
                "Notificação de teste de dispositivo offline.",
            )
            messages.success(request, "Teste offline enviado!")
        elif action == "test_cleaning":
            check_and_notify_dispenser_cleaning(request.user, settings_obj.dispenserCleaningReminderDays)
            messages.success(request, "Teste de limpeza do comedouro enviado!")
        elif action == "mark_cleaned":
            settings_obj.lastDispenserCleaningReminderAt = timezone.now()
            settings_obj.save(update_fields=["lastDispenserCleaningReminderAt"])
            create_notification(
                request.user,
                "dispenser_cleaning",
                "✅ Limpeza do comedouro concluída",
                "A limpeza do comedouro foi registrada e o próximo lembrete foi reiniciado.",
            )
            messages.success(request, "Limpeza do comedouro registrada no histórico.")
        return redirect("web_notifications")

    check_and_notify_dispenser_cleaning(request.user)

    # Paginação: 10 notificações por página
    paginator = Paginator(notifications_list, 10)
    page_number = request.GET.get("page")
    notifications = paginator.get_page(page_number)
    
    # Verificar se há notificações não lidas
    notifications.has_unread = notifications_list.filter(isRead=False).exists()

    return render(
        request,
        "api/notifications.html",
        {
            **nav_context(request),
            "settings": settings_obj,
            "notifications": notifications,
        },
    )


@login_required(login_url="/login/")
@require_http_methods(["GET", "POST"])
def dispenser_cleaning_page(request):
    settings_obj, _ = NotificationSettings.objects.get_or_create(user=request.user)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save_cleaning_settings":
            for field in ("emailDispenserCleaning", "pushDispenserCleaning"):
                setattr(settings_obj, field, field in request.POST)
            settings_obj.dispenserCleaningReminderDays = int(request.POST.get("dispenserCleaningReminderDays", 3))
            settings_obj.save()
            messages.success(request, "Configuração de limpeza salva!")
        elif action == "test_cleaning":
            notification = check_and_notify_dispenser_cleaning(request.user, settings_obj.dispenserCleaningReminderDays)
            if notification:
                messages.success(request, "Lembrete de limpeza enviado com sucesso.")
            else:
                messages.info(request, "O lembrete foi disparado recentemente; tente novamente após o intervalo configurado.")
        elif action == "mark_cleaned":
            settings_obj.lastDispenserCleaningReminderAt = timezone.now()
            settings_obj.save(update_fields=["lastDispenserCleaningReminderAt"])
            create_notification(
                request.user,
                "dispenser_cleaning",
                "✅ Limpeza do comedouro concluída",
                "A limpeza do comedouro foi registrada e o próximo lembrete foi reiniciado.",
            )
            messages.success(request, "Registro de limpeza atualizado. Próximo lembrete será agendado a partir de agora.")
        return redirect("web_dispenser_cleaning")

    notification = check_and_notify_dispenser_cleaning(request.user)
    if notification:
        messages.info(request, "Foi enviado um lembrete para limpar o comedouro.")

    return render(
        request,
        "api/dispenser_cleaning.html",
        {
            **nav_context(request),
            "settings": settings_obj,
            "reminder_days": settings_obj.dispenserCleaningReminderDays,
        },
    )


# --- Profile ---


@login_required(login_url="/login/")
@require_http_methods(["GET", "POST"])
def profile_page(request):
    user = CustomUser.objects.get(pk=request.user.pk)
    if request.method == "POST":
        user.first_name = request.POST.get("first_name", "")
        user.last_name = request.POST.get("last_name", "")
        user.email = request.POST.get("email", "")
        user.bio = request.POST.get("bio", "") or None
        user.cpf = request.POST.get("cpf", "") or None
        user.telefone = request.POST.get("telefone", "") or None
        user.profissao = request.POST.get("profissao", "") or None
        user.cidade = request.POST.get("cidade", "") or None
        user.estado = request.POST.get("estado", "") or None
        if request.FILES.get("photo"):
            user.photo = request.FILES["photo"]
        user.save()
        messages.success(request, "Perfil atualizado!")
        return redirect("web_profile")
    ctx = {**nav_context(request), "profile_user": user}
    ctx["user_photo_url"] = user_photo_url(user, request)
    return render(request, "api/profile.html", ctx)


def not_found(request, exception=None):
    return render(request, "api/404.html", {"show_nav": request.user.is_authenticated}, status=404)

@login_required
def dispenser_control_view(request):
    return render(request, 'api/dispenser_control.html')