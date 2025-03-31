import requests
import logging
import time
from .main import api_queue, load_config

logger = logging.getLogger('SMSForwarder')

def send_to_api_providers(api_providers, sender, timestamp, message, provider_name=None):
    """Send requests to API providers"""
    success = False
    config = load_config()
    default_timeout = config.get("default_timeout", 10)
    
    selected_providers = [p for p in api_providers if (provider_name and p["name"] == provider_name) or 
                         (not provider_name and p.get("default", False))]
    
    for provider in selected_providers:
        try:
            method = provider.get("method", "POST").upper()
            endpoint = provider["endpoint"].format(sender=sender, timestamp=timestamp, message=message)
            headers = {k: v.format(sender=sender, timestamp=timestamp, message=message) 
                      for k, v in provider.get("headers", {}).items()}
            payload = {k: v.format(sender=sender, timestamp=timestamp, message=message) if isinstance(v, str) else v 
                      for k, v in provider.get("payload", {}).items()}
            timeout = provider.get("timeout", default_timeout)

            if method == "POST":
                response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
            elif method == "GET":
                response = requests.get(endpoint, headers=headers, params=payload if payload else None, timeout=timeout)
            elif method == "PUT":
                response = requests.put(endpoint, headers=headers, json=payload, timeout=timeout)
            else:
                raise ValueError(f"Unsupported method: {method}")

            response.raise_for_status()
            success = True
        except Exception as e:
            logger.error(f"Failed to send to {provider['name']} API: {e}")
    return success

def api_forward_worker(api_providers, db_file):
    """Worker thread for API forwarding"""
    config = load_config()
    global_max_retries = config.get("max_retries", 3)
    
    while True:
        sender, timestamp, message, sms_id, retry_count, provider = api_queue.get()
        
        if not api_providers:
            api_queue.task_done()
            continue
        
        success = send_to_api_providers(api_providers, sender, timestamp, message, provider)
        
        from .db import mark_as_forwarded
        from .main import failed_services, notify_failure
        
        if success:
            if sms_id:
                mark_as_forwarded(db_file, sms_id, api_forwarded=True)
            if "API" in failed_services:
                failed_services.remove("API")
        else:
            retry_count += 1
            max_retries = max(provider.get("max_retries", global_max_retries) for provider in api_providers)
            if retry_count < max_retries:
                time.sleep(5 * retry_count)
                api_queue.put((sender, timestamp, message, sms_id, retry_count, provider))
            else:
                if "API" not in failed_services:
                    failed_services.add("API")
                    if sms_id:
                        notify_failure("API", sms_id)
        
        api_queue.task_done()