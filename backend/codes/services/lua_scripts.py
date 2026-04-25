"""Lua scripts wykonywane atomowo w Redisie."""

# Atomowo: sprawdza czy kod ma status ACTIVE, jeśli tak → zmienia na USED
# zachowując TTL. Zwraca:
#   "NOT_FOUND"      — klucz nie istnieje
#   "ALREADY_USED"   — kod ma już status != ACTIVE
#   <JSON payload>   — sukces
MARK_USED_SCRIPT = """
local key = KEYS[1]
local data = redis.call('GET', key)
if not data then
    return 'NOT_FOUND'
end
local payload = cjson.decode(data)
if payload.status ~= 'ACTIVE' then
    return 'ALREADY_USED'
end
local ttl = redis.call('PTTL', key)
payload.status = 'USED'
local new_data = cjson.encode(payload)
if ttl > 0 then
    redis.call('SET', key, new_data, 'PX', ttl)
else
    redis.call('SET', key, new_data)
end
return new_data
"""
