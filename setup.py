from setuptools import setup, find_packages

setup(
    name="gmail-hubspot-sync",
    version="1.0.0",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=[
        "google-auth>=2.29.0",
        "google-auth-oauthlib>=1.2.0",
        "google-auth-httplib2>=0.2.0",
        "google-api-python-client>=2.128.0",
        "hubspot-api-client>=9.0.0",
        "python-dotenv>=1.0.1",
    ],
    entry_points={"console_scripts": ["gmail-hubspot-sync=main:main"]},
)
