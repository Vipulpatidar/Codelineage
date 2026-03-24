from google import genai

client = genai.Client(
    api_key="AIzaSyAFMIMk3LMIS_djL4b3gXQcoYmW8JUOGp4"
)

response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="what model are you and what is your context window?"
)

print(response.text)