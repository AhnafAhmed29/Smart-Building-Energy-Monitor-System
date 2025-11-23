import requests, hmac, hashlib, time

ACCESS_ID = 'w3nx989guhntvfyeqm9t'
ACCESS_SECRET = 'bd6e516ea10f4a73a002060626003f97'
DEVICE_ID = 'd70da46fd096b7afeabgvb'
BASE_URL = 'https://openapi.tuyain.com'  # change to your region if needed

def sign(method, path, body='', token=''):
    t = str(int(time.time() * 1000))
    msg = ACCESS_ID + (token or '') + t + f"{method}\n{hashlib.sha256(body.encode()).hexdigest()}\n\n{path}"
    return t, hmac.new(ACCESS_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest().upper()

# Step 1: Get access token
t, sig = sign('GET', '/v1.0/token?grant_type=1')
r = requests.get(BASE_URL + '/v1.0/token?grant_type=1',
                 headers={'client_id': ACCESS_ID, 'sign': sig, 't': t, 'sign_method': 'HMAC-SHA256'})
print("Token response:", r.text)

token = r.json()['result']['access_token']

# Step 2: Get device status
t, sig = sign('GET', f'/v1.0/devices/{DEVICE_ID}/status', '', token)
headers = {'client_id': ACCESS_ID, 'access_token': token, 'sign': sig, 't': t, 'sign_method': 'HMAC-SHA256'}
r = requests.get(BASE_URL + f'/v1.0/devices/{DEVICE_ID}/status', headers=headers)
print("Device response:", r.text)
