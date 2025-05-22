# create_requirements.py
def create_requirements_file():
    dependencies = [
        "feedparser",
        "requests",
        "beautifulsoup4",
        "praw",
        "python-dateutil",
        "langdetect",
        "pycountry"
    ]
    with open("requirements.txt", "w", encoding="utf-8") as f:
        for dep in dependencies:
            f.write(f"{dep}\n")
    print("Created requirements.txt with dependencies.")

if __name__ == "__main__":
    create_requirements_file()
