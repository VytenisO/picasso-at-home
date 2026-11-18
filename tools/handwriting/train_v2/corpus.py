"""Text for the synthetic training samples: common English and Norwegian words, pseudo-words from a letter-bigram
model (so every letter pair shows up), numbers and punctuation. Held-out words are removed by the caller."""
import random

EN = """the be to of and a in that have it for not on with he as you do at this but his by from they we say her she or
an will my one all would there their what so up out if about who get which go me when make can like time no just him
know take people into year your good some could them see other than then now look only come its over think also back
after use two how our work first well way even new want because any these give day most us is was are were been has
had did said made went came took got found told thought left right small large great little long old big high young
early last next few public able home house water room hand mother father world school night point city family story
book word side head eye face place part light paper music money table friend letter garden window door street market
morning evening summer winter spring autumn yellow green blue black white orange purple brown happy quiet gentle bright
quick lazy slow simple clear strong warm cold sweet dark river ocean mountain forest island bridge station kitchen
coffee bread butter cheese apple orange lemon sugar salt pepper dinner lunch breakfast doctor teacher student pencil
plotter writing drawing machine robot motor engine signal number answer question reason moment minute second hour week
month birthday holiday weekend journey travel visit wonder whisper laugh smile listen remember forget believe imagine
explain wish hope dream sleep jump climb dance swim walk run sit stand write read draw sing play build open close
begin finish start stop try ask call help keep hold bring turn move live feel seem show hear let mean put set learn
change follow carry watch talk tell eat drink cook clean wash wear pay buy sell send receive wait stay lose win break
exactly quietly almost always never often sometimes perhaps maybe together tomorrow yesterday tonight today already
zebra quiz jazz fizz buzz puzzle frozen dozen lazy crazy wizard oxygen box fox six tax relax next extra index exam
squeeze quite queen quote square equal liquid unique jungle judge major object project subject enjoy""".split()

NO = """og i det er som en på til med av for ikke den at har jeg de var seg om et men så vi kan han hun skal fra hadde
ble også etter vil eller bare være noe nå hva når mot dette alle andre over mange kommer år ut sier blir to må denne
inn sin her hvor hvis fordi uten mellom under hele norge store tid dag dager gang ganger mennesker barn hus hjem skole
arbeid jobb penger bok brev papir penn blyant tegning maskin vann melk kaffe te brød ost smør egg fisk kjøtt grønnsaker
eple pære banan sukker salt middag frokost lunsj kveld morgen natt uke måned høst vinter vår sommer snø regn sol vind
fjell skog sjø elv innsjø hav båt bil tog buss fly vei gate by land verden himmel stjerne måne lys mørke farge rød blå
grønn gul hvit svart brun lilla oransje glad trist rolig sint redd snill stor liten lang kort gammel ung ny varm kald
god dårlig fin pen stygg rask treg lett tung høy lav sterk svak ærlig ære ærend være lære bære nære sære tære været
øre øye øl øst øve øvelse øke økt ødelagt ønske ønsker øvre søster bror mor far venn venner kjæreste familie hund katt
fugl hest ku sau gris mus bjørn ulv rev elg reinsdyr gå løpe hoppe svømme klatre danse synge spille lese skrive tegne
male bygge åpne lukke begynne slutte prøve spørre svare hjelpe holde bringe snu flytte leve føle virke vise høre la
bety sette lære følge bære se snakke fortelle spise drikke lage vaske kjøpe selge sende få vente bo tape vinne bryte
åtte år åker ånd ånde åpen åsen gård gås grå blå små må få nå så på hånd sånn kråke låve både også ikkje kjøre kjøkken
kjenne kjole skje skjorte skjønn sjø sjef sjanse sjelden hjul hjelp hjerne gjøre gjerne gjest tjue tjene""".split()


def _bigram_model(words):
    trans = {}
    for w in words:
        w = "^" + w.lower() + "$"
        for a, b in zip(w, w[1:]):
            trans.setdefault(a, []).append(b)
    return trans


class Corpus:
    def __init__(self, extra_words=(), exclude=(), seed=0):
        ex = {w.lower() for w in exclude}
        base = [w for w in list(EN) + list(NO) + list(extra_words) if w.lower() not in ex and w]
        self.words = sorted(set(base))
        self.exclude = ex
        self.trans = _bigram_model(self.words)
        self.rng = random.Random(seed)

    def pseudo(self, rng):
        for _ in range(20):
            w, c = "", "^"
            while len(w) < 10:
                c = rng.choice(self.trans[c])
                if c == "$":
                    break
                w += c
            if 2 <= len(w) and w not in self.exclude:
                return w
        return rng.choice(self.words)

    def word(self, rng):
        r = rng.random()
        if r < 0.62:
            w = rng.choice(self.words)
        elif r < 0.88:
            w = self.pseudo(rng)
        elif r < 0.94:
            w = str(rng.randrange(0, 10 ** rng.randint(1, 4)))
            if rng.random() < 0.2:
                w = w + rng.choice([":", ",", ".", "-"]) + str(rng.randrange(0, 100))
        else:
            w = rng.choice(["(", "\"", "'"]) + rng.choice(self.words) + rng.choice([")", "\"", "'", "!", "?"])
        if w[0].isalpha():
            if rng.random() < 0.15:
                w = w[0].upper() + w[1:]
            elif rng.random() < 0.01:
                w = w.upper()
            if rng.random() < 0.12:
                w += rng.choice([".", ",", "!", "?", ":", ";"])
        return w

    def text(self, rng, max_words):
        n = rng.randint(1, max_words)
        return " ".join(self.word(rng) for _ in range(n))
