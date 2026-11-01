"""
Views para controle do servo motor via ESP32
"""
import time
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status # pyrefly: ignore [missing-import]
from rest_framework.decorators import api_view, permission_classes # pyrefly: ignore [missing-import]
from rest_framework.permissions import AllowAny, IsAuthenticated # pyrefly: ignore [missing-import]
from rest_framework.response import Response # pyrefly: ignore [missing-import]
from django.shortcuts import get_object_or_404
from django.utils import timezone
from .models import Pet, FeedingHistory, DeviceStatus, CustomUser
from .iot_client import esp32_client
from .notification_utils import notify_feeding_completed, check_and_notify_low_food, notify_device_offline


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@csrf_exempt
def trigger_feeding(request):
    """
    Endpoint para acionar alimentação manual via ESP32
    
    POST /api/feed/
    {
        "pet_id": 1,
        "quantity": 50,
        "notes": "Alimentação manual"
    }
    """
    pet_id = request.data.get('pet_id')
    quantity = request.data.get('quantity', 50)
    notes = request.data.get('notes', 'Manual')
    
    # Validar pet
    pet = get_object_or_404(Pet, id=pet_id)
    channel = request.data.get('channel')
    if channel is not None:
        try:
            channel = int(channel)
        except Exception:
            channel = None
    if channel is None and pet.dispenser_channel is not None:
        channel = pet.dispenser_channel
    
    # Verificar se ESP32 está online
    if not esp32_client.is_online():
        return Response({
            'success': False,
            'error': 'Dispositivo ESP32 offline ou não responde',
            'device_status': 'offline'
        }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    
    # Enviar comando para ESP32
    feeding_id = f"manual_{pet_id}_{int(time.time())}"
    result = esp32_client.dispense_food(
        quantity=quantity,
        duration_ms=calculate_duration(quantity),
        feeding_id=feeding_id,
        channel=channel,
    )
    
    if result.get('success'):
        # Registrar no histórico
        feeding = FeedingHistory.objects.create(
            pet=pet,
            quantity=quantity,
            feedingType='manual',
            success=True,
            notes=f"ESP32: {feeding_id}. {notes}"
        )
        
        # Atualizar status do dispositivo para o usuário correto
        update_device_status(pet.user, result)
        
        # Enviar notificação de alimentação concluída
        try:
            notify_feeding_completed(
                user=pet.user,
                pet_name=pet.name,
                quantity=quantity
            )
            
            # Verificar se precisa notificar sobre ração baixa
            food_level_after = result.get('food_level_after', 100)
            check_and_notify_low_food(pet.user, food_level_after)
        except Exception as e:
            # Não falhar a alimentação se notificação falhar
            print(f"Erro ao enviar notificação: {e}")
        
        return Response({
            'success': True,
            'feeding_id': feeding.id,
            'quantity': quantity,
            'device_response': result,
            'timestamp': feeding.timestamp
        })
    else:
        # Registrar tentativa falha
        FeedingHistory.objects.create(
            pet=pet,
            quantity=quantity,
            feedingType='manual',
            success=False,
            notes=f"Falha ESP32: {result.get('error', 'Unknown error')}"
        )
        
        return Response({
            'success': False,
            'error': result.get('error', 'Falha ao comunicar com ESP32'),
            'device_response': result
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_device_status(request):
    """
    Retorna status do ESP32 mascarando o IP da placa e atualizando o banco
    """
    channel_param = request.query_params.get('channel')
    channel = int(channel_param) if channel_param is not None else 0
    
    esp32_status = esp32_client.get_status(channel)
    is_online = bool(esp32_status and esp32_status.get('online', False))
    food_level = esp32_status.get('food_level', 0) if is_online else 0
    
    # Atualiza a tabela DeviceStatus no banco com dados reais
    upsert_device_status(
        user=request.user,
        channel=channel,
        defaults={
            'isOnline': is_online,
            'foodLevel': food_level,
            'batteryLevel': 100,
            'lastSeen': timezone.now()
        }
    )
    
    if not is_online:
        return Response({
            'online': False,
            'simulated': False,
            'channel': channel,
            'error': 'Dispositivo offline'
        }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    
    # Resposta limpa sem expor o IP da placa ao navegador
    return Response({
        'online': True,
        'simulated': False,
        'channel': channel,
        'food_level': food_level,
        'battery_level': 100,
        'is_dispensing': esp32_status.get('is_dispensing', False),
        'device_id': esp32_status.get('device_id', 'petfeeder_001'),
        'last_feeding': esp32_status.get('last_feeding')
    })

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def calibrate_servo(request):
    """
    Endpoint para calibrar o servo motor
    
    POST /api/servo/calibrate/
    {
        "position": 45  // 0-90 graus
    }
    """
    position = request.data.get('position', 0)
    position = max(0, min(90, position))  # Clamp 0-90
    
    if not esp32_client.is_online():
        return Response({
            'success': False,
            'error': 'ESP32 offline'
        }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    
    result = esp32_client.calibrate_servo(position)
    
    return Response({
        'success': result.get('success', False),
        'position': position,
        'device_response': result
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def test_connection(request):
    """
    Testa a conexão com o ESP32
    
    POST /api/device/test/
    """
    is_online = esp32_client.is_online()
    device_info = esp32_client._request("GET", "/") if is_online else None
    
    return Response({
        'connected': is_online,
        'device_info': device_info,
        'esp32_url': esp32_client.base_url
    })


# ===== FUNÇÕES AUXILIARES =====

def calculate_duration(quantity_grams: int) -> int:
    """
    Calcula tempo de abertura do servo baseado na quantidade desejada.
    Para 100g, usa 3000 ms a 90°; para 200g, usa 6000 ms a 90°.
    """
    duration_ms = max(0, int(quantity_grams * 30))
    return duration_ms


def update_device_status(user, esp32_response: dict):
    """Atualiza o registro DeviceStatus no banco de dados para o usuário especificado"""
    channel = esp32_response.get('channel')
    upsert_device_status(
        user=user,
        channel=channel,
        defaults={
            'isOnline': True,
            'foodLevel': esp32_response.get('food_level_after', 0),
            'lastSeen': timezone.now()
        }
    )


def upsert_device_status(user, channel=None, defaults=None):
    """Atualiza um DeviceStatus existente ou cria um novo, mesmo se houver registros duplicados."""
    defaults = defaults or {}
    queryset = DeviceStatus.objects.filter(user=user, channel=channel)
    device = queryset.order_by('-updatedAt', '-lastSeen', '-id').first()

    if device is None:
        return DeviceStatus.objects.create(user=user, channel=channel, **defaults)

    for key, value in defaults.items():
        setattr(device, key, value)

    device.save()

    stale_devices = queryset.exclude(pk=device.pk)
    if stale_devices.exists():
        stale_devices.delete()

    return device


def get_default_user():
    """Retorna usuário admin/padrão para desenvolvimento"""
    return CustomUser.objects.filter(username="admin").first() or CustomUser.objects.first()


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def update_simulator_settings(request):
    """
    Atualiza ou lê as configurações forçando o modo de hardware real
    GET /api/device/simulator/settings/
    POST /api/device/simulator/settings/
    """
    if request.method == 'POST':
        result = esp32_client.update_simulator(request.data)
        return Response(result)
    
    # Força desativação da simulação e mascara o IP real
    return Response({
        "simulator_enabled": False,
        "is_simulated": False,
        "ip": "Conectado (Gateway Interno)",
        "state": {
            "online": True,
            "ip": "Conectado (Gateway Interno)",
            "device_id": "petfeeder_001"
        }
    })

@api_view(['POST'])
@permission_classes([IsAuthenticated])
@csrf_exempt
def refill_food_container(request):
    """
    Endpoint para reabastecer o reservatório de ração
    
    POST /api/device/refill/
    {
        "food_level": 100  // Opcional, padrão é 100%
    }
    """
    food_level = request.data.get('food_level', 100)
    food_level = max(0, min(100, int(food_level)))  # Clamp 0-100
    
    # Atualizar simulador
    result = esp32_client.update_simulator({"food_level": food_level})
    
    # Atualizar status do dispositivo no banco para o usuário autenticado
    upsert_device_status(
        user=request.user,
        channel=None,
        defaults={
            'isOnline': True,
            'foodLevel': food_level,
            'lastSeen': timezone.now()
        }
    )
    
    # Se nível de ração estava baixo e agora está OK, criar notificação de sucesso
    if food_level > 20:
        try:
            from .notification_utils import create_notification
            create_notification(
                user=request.user,
                notification_type='general',
                title='🎉 Reservatório reabastecido!',
                message=f'O reservatório de ração foi preenchido para {food_level}%.',
                send_email=False,
                send_push=True
            )
        except Exception as e:
            print(f"Erro ao criar notificação de reabastecimento: {e}")
    
    return Response({
        'success': True,
        'food_level': food_level,
        'message': f'Reservatório atualizado para {food_level}%'
    })

