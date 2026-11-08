import math
import pytest
from kicktrans.config import Config

@pytest.mark.parametrize("values", [
    {"home_lang":"日本語"}, {"foreign_lang":""}, {"home_lang":"en?"},
    {"output_prefix":""}, {"output_prefix":" "}, {"output_prefix":"x"*33},
    {"channel_slug":"bad/name"}, {"channel_slug":""},
    {"min_post_interval_sec":-1}, {"min_post_interval_sec":math.nan},
    {"max_queue":0}, {"max_queue":True}, {"max_event_age_sec":0},
    {"webhook_port":8081.0}, {"receive_method":"other"},
])
def test_settings_reject_invalid_values_without_echoing_input(values):
    with pytest.raises(ValueError):
        Config(**values)

def test_settings_support_neo_languages_and_direct_connection():
    cfg=Config(home_lang="ja",foreign_lang="en",trans_to_home_only=False)
    assert cfg.receive_method=="websocket"
    assert cfg.webhook_port==8081
    assert cfg.live_posting is False
