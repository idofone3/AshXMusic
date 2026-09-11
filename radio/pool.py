"""Hindi hits radio pool — the autoplay mix (YT-Music-style "start radio").

Each entry: (search_query, title_keyword, artist_keyword).
The station searches the query on YouTube Music, then picks the first
result whose title/artist match the keywords and passes the remix filter.
"""

# words that mark covers / remixes / unofficial edits — never autoplay them
BADWORDS = (
    "remix", "lofi", "lo-fi", "slowed", "reverb", "cover", "instrumental",
    "karaoke", "nightcore", "8d", "sped up", "reprise", "acoustic",
    "encore", "unplugged", "live", "chirantan", "lyrical video",
)

# (query, title_kw, artist_kw) — artist_kw may be "" to allow any artist
POOL = [
    # ---- 2019-2025 modern hits ----
    ("Kesariya Arijit Singh", "kesariya", "arijit"),
    ("Apna Bana Le Arijit Singh", "apna bana", "arijit"),
    ("Heeriye Jasleen Royal Arijit Singh", "heeriye", ""),
    ("Satranga Arijit Singh Animal", "satranga", "arijit"),
    ("Tere Vaaste Varun Jain", "tere vaaste", ""),
    ("Chaleya Arijit Singh Anirudh", "chaleya", ""),
    ("Jhoome Jo Pathaan Arijit Singh", "jhoome", ""),
    ("Besharam Rang Shilpa Rao", "besharam rang", ""),
    ("Khoya Khoya Shaswat", "khoya khoya", ""),
    ("Tere Hawaale Arijit Singh", "hawaale", ""),
    ("Show Me The Thumka", "thumka", ""),
    ("Piya O Re Piya Atif Aslam", "piya o re", ""),
    ("Raatan Lambiyan Jubin Nautiyal", "raatan", ""),
    ("Ranjha Jasleen Royal B Praak", "ranjha", ""),
    ("Machhli坐标", "machhli", ""),  # placeholder replaced below
    ("Kahani Suno 2.0 Kaifi Khalil", "kahani suno", "kaifi"),
    ("Pasoori Ali Sethi Shae Gill", "pasoori", ""),
    ("O Bedardeya Arijit Singh", "bedardeya", ""),
    ("Tere Pyaar Mein Arijit Singh", "tere pyaar", ""),
    ("Nai Chaida Sonu Kakkar", "nai chaida", ""),
    ("Malang Sajna Sachet Parampara", "malang sajna", ""),
    ("Tere Vaaste Falana", "tere vaaste", ""),
    ("Rangisari Varsha Bhabhra", "rangisari", ""),
    ("Doobey Doobey Olli Ella", "doobey", ""),
    ("Rasiya Brahmastra Arijit", "rasiya", ""),
    ("Deva Deva Brahmastra Arijit", "deva deva", ""),
    ("Dance Ka Bhoot Arijit Singh", "dance ka bhoot", ""),
    ("Aashiyan Barfi", "aashiyan", ""),
    ("Manike Yo Yo Honey Singh", "manike", ""),
    ("Mudhal Nee Mudivum Naa", "mudhal", ""),
]

# NOTE: keep the list clean & purely latin; append more below
POOL = [p for p in POOL if "坐标" not in p[0]]

POOL += [
    # ---- 2010s big ones ----
    ("Tum Hi Ho Arijit Singh", "tum hi ho", "arijit"),
    ("Kabira Tochi Raina Rekha Bhardwaj", "kabira", ""),
    ("Channa Mereya Arijit Singh", "channa mereya", "arijit"),
    ("Agar Tum Saath Ho Alka Yagnik Arijit", "agar tum saath", ""),
    ("Ilahi Arijit Singh", "ilahi", "arijit"),
    ("Subhanallah Arijit Singh", "subhanallah", ""),
    ("Raabta Arijit Singh Agent Vinod", "raabta", "arijit"),
    ("Samjhawan Arijit Singh Shreya", "samjhawan", ""),
    ("Sooraj Dooba Hain Arijit Singh", "sooraj dooba", ""),
    ("Gerua Arijit Singh", "gerua", "arijit"),
    ("Janam Janam Arijit Singh", "janam janam", "arijit"),
    ("Hawayein Arijit Singh", "hawayein", ""),
    ("Zaalima Arijit Singh Harshdeep", "zaalima", ""),
    ("Enna Sona Arijit Singh", "enna sona", ""),
    ("Bolna Arijit Singh Arijit", "bolna", ""),
    ("Nashe Si Chadh Gayi Arijit Singh", "nashe si chadh", ""),
    ("Paniyon Sa Atif Aslam", "paniyon sa", ""),
    ("Tera Ban Jaunga Akhil Sachdeva", "tera ban jaunga", "akhil"),
    ("Khairiyat Arijit Singh", "khairiyat", "arijit"),
    ("Shayad Arijit Singh", "shayad", "arijit"),
    ("Bhula Dena Mustafa Zahid", "bhula dena", ""),
    ("Tujhe Bhula Diya Mohit Chauhan", "tujhe bhula", ""),
    ("Tum Se Hi Mohit Chauhan", "tum se hi", "mohit"),
    ("Pehla Nasha Udit Narayan Sadhana Sargam", "pehla nasha", ""),
    ("Tera Yaar Hoon Main Arijit Singh", "tera yaar hoon", ""),
    ("Dil Diyan Gallan Atif Aslam", "dil diyan gallan", ""),
    ("Moh Moh Ke Dhaage Papon", "moh moh", ""),
    ("Phir Le Aya Dil Arijit Singh", "phir le aya", ""),
    ("Muskurane Arijit Singh Citylights", "muskurane", ""),
    ("Sunn Raha Hai Arijit Singh", "sunn raha", ""),
    ("Piya Aaye Na Arijit Singh", "piya aaye", ""),
    ("Aasan Nahin Yahan Arijit Singh", "aasan nahin", ""),
    ("Mere Naam Tu Abhay Jodhpurkar", "mere naam tu", ""),
    ("First Class Arijit Singh", "first class", "arijit"),
    ("Ghungroo Arijit Singh Shilpa Rao", "ghungroo", ""),
    ("Dilbar Neha Kakkar Dhvani", "dilbar", ""),
    ("O Saki Saki Neha Kakkar Tulsi", "saki saki", ""),
    ("Aankh Marey Neha Kakkar Mika", "aankh marey", ""),
    ("Kala Chashma Badshah Neha Kakkar", "kala chashma", ""),
    ("Kar Gayi Chull Badshah", "chull", ""),
    ("Saturday Saturday Indeep Bakshi", "saturday saturday", ""),
    ("Gali Gali Neha Kakkar", "gali gali", "neha"),
    ("Dholida Garba Janhvi", "dholida", ""),
    ("Nagada Sang Dhol Shreya Ghoshal Osman Mir", "nagada sang", ""),
    ("Laila Main Laila Shruti Haasan", "laila main laila", ""),
    ("Muqabla A R Rahman Yash Narvekar", "muqabla", ""),
    ("Seeti Maar Mika Singh", "seeti maar", ""),
    ("Srivalli Sid Sriram", "srivalli", ""),
    ("Butta Bomma Armaan Malik", "butta bomma", "armaan"),
    ("Nachde Ne Saare Harjas Kaur", "nachde ne saare", ""),
    ("Kudi Nu Nachne De Vishal Dadlani", "kudi nu nachne", ""),
    ("Bom Diggy Zack Knight Jasmin", "bom diggy", ""),
    ("Dil Chori Harrdy Sandhu", "dil chori", "harrdy"),
    ("Sawan Mein Lag Gayi Aag Woodpecker", "sawan mein lag", ""),
    ("Illegal Weapon 2.0 Garry Sandhu", "illegal weapon", ""),
    ("Tera Ghata Gajendra Verma", "tera ghata", ""),
    ("Vaaste Dhvani Bhanushali", "vaaste", ""),
    ("Dilbar Dhvani Bhanushali", "dilbar", ""),
    ("Na Ja Pav Dharia", "na ja", "pav"),
    ("Ik Vaari Aa Raabta Arijit", "ik vaari aa", ""),
    ("Sajan Radar Ne Bhawe", "sajan", ""),
    ("Chogada Darshan Raval", "chogada", "darshan"),
    ("Kamariya Darshan Raval", "kamariya", ""),
    ("Ek Ladki Ko Dekha Toh Aisa Laga", "ek ladki ko dekha", ""),
    ("Tera Hone Laga Hoon Atif Aslam", "tera hone laga", ""),
    ("Tera Hoke Hoon Na Arijit", "tera hoke", ""),
    ("Bezubaan Phir Se", "bezubaan", ""),
    ("Sunn Beliya Shreyas Puranik", "sunn beliya", ""),
    ("Makhna Yo Yo Honey Singh", "makhna", "honey"),
    ("Ude Dil Befikre Benny Dayal", "ude dil befkire", ""),
    ("Manma Emotion Jaage Dil", "manma emotion", ""),
    ("Sau Aasmaan Neeti Mohan", "sau aasmaan", ""),
    ("Sapna Jahan Sonu Nigam", "sapna jahan", ""),
    ("Matargashti Mohit Chauhan", "matargashti", ""),
    ("Agar Tum Mil Jao Zeenat Aman? no", "hum mile", ""),
]

POOL = [p for p in POOL if "?" not in p[0]]

POOL += [
    # ---- 2000s & classics for depth ----
    ("Kal Ho Naa Ho Sonu Nigam", "kal ho naa ho", "sonu"),
    ("Tumhi Dekho Naa Sonu Nigam", "tumhi dekho", ""),
    ("Mitwa Sonu Nigam Shreya", "mitwa", ""),
    ("Tujh Mein Rab Dikhta Hai Roop Kumar", "tujh mein rab", ""),
    ("Jab Se Tere Naina Shaan", "jab se tere naina", ""),
    ("Maa Taare Zameen Par", "maa ", ""),
    ("Tere Bin Bas Ek Pal Sanam", "tere bin", ""),
    ("Kun Faya Kun A R Rahman Javed Ali", "kun faya kun", ""),
    ("Tum Tak Javed Ali", "tum tak", ""),
    ("Ishq Sufiyana Kamal Khan", "ishq sufiyana", ""),
    ("Dil Ibadat KK Tum Mile", "dil ibadat", ""),
    ("Zara Sa KK", "zara sa", ""),
    ("Alvida KK", "alvida", ""),
    ("Meri Maa Yaariyan", "meri maa", ""),
    ("Tujhe Sochta Hoon KK Jannat", "tujhe sochta", ""),
    ("Haan Tu Hai KK", "haan tu hai", ""),
    ("Soniyo Sonu Nigam", "soniyo", ""),
    ("Abhi Abhi Jism 2", "abhi abhi", ""),
    ("Yeh Dooriyan Mohit Chauhan", "yeh dooriyan", ""),
    ("Pee Loon Mohit Chauhan", "pee loon", ""),
    ("Tune Jo Na Kaha Mohit Chauhan", "tune jo na kaha", ""),
    ("Tum Mile Neeraj Shridhar", "tum mile", "neeraj"),
    ("Pehli Nazar Mein Atif Aslam", "pehli nazar", ""),
    ("Tu Jaane Na Atif Aslam", "tu jaane na", ""),
    ("Bakhuda Tumhi Ho Atif Aslam", "bakhuda", ""),
    ("O Meri Laila Atif Aslam", "o meri laila", ""),
    ("Le Ja Tu Mujhe Faltu", "le ja tu mujhe", ""),
    ("Character Dheela Atif Aslam", "character dheela", ""),
    ("Allah Duhai Hai Zed", "allah duhai", ""),
    ("Main Hoon Hero Tera Armaan Malik", "main hoon hero tera", ""),
    ("Wajah Tum Ho Armaan Malik", "wajah tum ho", ""),
    ("Bheegh Loon Kunal Ganjawala", "bheegh loon", ""),
    ("Bhula Dena Mujhe Atif", "bhula dena", ""),
    ("Saiyaara Mohit Chauhan", "saiyaara", ""),
    ("Darmiyaan Javed Ali Kavita", "darmiyaan", ""),
    ("Aye Khuda Murder 2", "aye khuda", ""),
    ("Maula Mere Maula Roop Kumar", "maula mere", ""),
    ("O Re Piya Rahat Fateh Ali Khan", "o re piya", "rahat"),
    ("Tere Mast Mast Do Nain Rahat", "tere mast mast", ""),
    ("Sajda Rahat Fateh Ali Khan", "sajda", "rahat"),
    ("Dagabaaz Rahat Fateh Ali Khan", "dagabaaz", ""),
    ("Mere Rashke Qamar Rahat Fateh Ali Khan", "rashke qamar", ""),
    ("Jag Ghoomeya Rahat Fateh Ali Khan", "jag ghoomeya", ""),
    ("Main Phir Bhi Tumko Chahunga Arijit", "main phir bhi", ""),
    ("Baat Ban Jaye Arijit Singh", "baat ban jaye", ""),
    ("Nazm Nazm Arko", "nazm nazm", ""),
    ("Humsafar Akhil Sachdeva", "humsafar", "akhil"),
    ("Lae Dooba Sunidhi Chauhan", "lae dooba", ""),
    ("Hichki Gal Mithi Mithi", "gal mithi", ""),
    ("Sadda Kutta Rocker? no", "sadda", ""),
]

POOL = [p for p in POOL if "?" not in p[0]]


def pick(rng, recent_ids):
    """Pick a random pool entry whose (kw) wasn't played recently."""
    import random
    entries = list(POOL)
    rng.shuffle(entries)
    for q, tkw, akw in entries:
        key = q.lower()
        if not any(key in r for r in recent_ids):
            return {"q": q, "tkw": tkw.lower(), "akw": akw.lower()}
    return {"q": random.choice(POOL)[0], "tkw": "", "akw": ""}
