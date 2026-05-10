"""
feature_flags.py — динамический мост к app/settings.

Все флаги читаются из UserSettings при каждом вызове — меняются через /settings без рестарта.
Использовать: import app.feature_flags as ff; ff.critic_enabled()
"""
import app.settings as _S


def _s():
    return _S.get()


# Функции (не @property — property не работает на уровне модуля)
def ideas_agent()       -> bool: return _s().module_ideas
def morning_digest()    -> bool: return _s().digest_enabled
def task_deadlines()    -> bool: return _s().task_deadlines
def reminder_warning()  -> bool: return _s().reminder_warning
def proactive_ideas()   -> bool: return _s().proactive_ideas
def proactive_activity() -> bool: return _s().proactive_activity
def proactive_silence() -> bool: return _s().proactive_silence
def persona_verbose()   -> bool: return _s().persona_verbose
def emotional_system()  -> bool: return _s().emotional_system
def critic_enabled()    -> bool: return _s().critic_enabled
def memory_enabled()    -> bool: return _s().memory_enabled
