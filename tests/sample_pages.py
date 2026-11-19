"""Offline sample pages shared by the extraction, profile and local-crawl
tests. The fictional company "Acme Widgets" uses the domain acme-widgets.co
(not a real site; never fetched -- pages are served from 127.0.0.1 or
parsed directly)."""

HOME = """<!DOCTYPE html>
<html lang="en-GB">
<head>
  <title>Acme Widgets | Industrial Widget Engineering</title>
  <meta name="description" content="Acme Widgets designs and manufactures precision widgets for manufacturers.">
  <meta name="keywords" content="widgets, engineering, manufacturing">
  <link rel="canonical" href="/">
  <meta property="og:title" content="Acme Widgets">
  <meta property="og:description" content="Precision widgets since 1999.">
  <meta property="og:type" content="website">
  <meta property="og:site_name" content="Acme Widgets">
  <meta name="twitter:card" content="summary">
  <style>.hero { color: red; } body { font-family: sans-serif; }</style>
  <script>var tracking = "track@analytics-vendor.io"; window.dataLayer = [];</script>
  <script type="application/ld+json">
  {"@context": "https://schema.org", "@graph": [
    {"@type": "ProfessionalService", "name": "Acme Widgets Ltd",
     "description": "Engineering firm specialising in custom industrial widgets.",
     "url": "https://acme-widgets.co/", "email": "hello@acme-widgets.co",
     "telephone": "+44 20 7946 0958", "foundingDate": "1999",
     "address": {"@type": "PostalAddress", "streetAddress": "12 Gear Street",
                 "addressLocality": "Manchester", "postalCode": "M1 2AB", "addressCountry": "GB"},
     "sameAs": ["https://www.linkedin.com/company/acme-widgets"]},
    {"@type": "Service", "name": "Widget Prototyping",
     "description": "Rapid prototypes of custom widgets in 10 days."}
  ]}
  </script>
  <script type="application/ld+json">{ this is not valid json </script>
</head>
<body>
  <header><nav>
    <a href="/">Home</a> <a href="/about">About us</a> <a href="/services/design">Design</a>
    <a href="/contact">Contact</a> <a href="/blog/widget-news">Blog</a>
  </nav></header>
  <main>
    <h1>Precision widgets for modern manufacturers</h1>
    <p>We help manufacturers and engineering teams reduce downtime with reliable widgets.</p>
    <p>Founded in 1999, our team has delivered more than 4,000 projects between 2019 - 2024 alone.</p>
    <h2>Our Services</h2>
    <div class="card"><h3>Custom Widget Design</h3><p>Bespoke widget designs engineered to your specification.</p></div>
    <div class="card"><h3>Widget Maintenance</h3><p>Preventive maintenance plans for installed widgets.</p></div>
    <h2>Industries we serve</h2>
    <ul><li>Automotive</li><li>Aerospace</li><li>Food processing</li></ul>
    <h2>Pricing overview</h2>
    <table><tr><th>Plan</th><th>Price</th></tr><tr><td>Basic</td><td>£99</td></tr></table>
    <figure><img src="/img/factory.jpg" alt="Our factory floor" title="Factory"><figcaption>Our Manchester factory</figcaption></figure>
    <img data-src="/img/lazy.png" alt="Lazy widget">
    <form><input type="email" placeholder="your@email.com"><button>Subscribe</button></form>
    <p style="display:none">hidden-secret@acme-widgets.co should not appear</p>
    <p>Partner sites: <a href="https://partner.example.org/page" rel="nofollow">Partner portal</a></p>
  </main>
  <footer>
    <address>12 Gear Street, Manchester M1 2AB</address>
    <p>Email us: <a href="mailto:sales@acme-widgets.co?subject=Hi">sales@acme-widgets.co</a></p>
    <p>Call: <a href="tel:+442079460958">+44 20 7946 0958</a> or phone 0161 496 0000</p>
    <p>Example address: someone@example.com</p>
    <a href="https://twitter.com/acmewidgets">Twitter</a>
    <a href="https://www.facebook.com/sharer/sharer.php?u=x">Share</a>
    <a href="https://www.instagram.com/acmewidgets/">Instagram</a>
  </footer>
</body>
</html>"""

ABOUT = """<html lang="en"><head><title>About us | Acme Widgets</title>
<meta name="description" content="Learn about Acme Widgets, a family-run widget engineering company."></head>
<body><nav><a href="/">Home</a></nav><main>
<h1>About Acme Widgets</h1>
<p>Acme Widgets is a family-run engineering company based in Manchester, building widgets for over twenty years.</p>
<p>We work with automotive and aerospace companies across Europe to keep production lines running.</p>
</main></body></html>"""

SERVICE_DESIGN = """<html><head><title>Custom Widget Design Services | Acme Widgets</title>
<meta name="description" content="Bespoke widget design from concept to production drawings."></head>
<body><main><h1>Custom Widget Design Services</h1>
<p>Our engineers turn your requirements into manufacturable widget designs.</p></main></body></html>"""

# URL contains "services" but the content does not present any service.
SERVICES_URL_ONLY = """<html><head><title>Company news | Acme Widgets</title></head>
<body><main><h1>Company news</h1><p>We moved to a new office this spring and threw a party.</p></main></body></html>"""

CONTACT = """<html><head><title>Contact | Acme Widgets</title></head>
<body><main><h1>Contact us</h1><p>Phone: +44 161 496 0001</p>
<p>Write to support@acme-widgets.co for help.</p></main></body></html>"""

JS_APP = """<html><head><title>App</title>
<script src="/static/a.js"></script><script src="/static/b.js"></script><script src="/static/c.js"></script>
</head><body><div id="root"></div><noscript>You need to enable JavaScript to run this app.</noscript></body></html>"""

ROBOTS = "User-agent: *\nDisallow: /private\n"

PRIVATE = "<html><head><title>SECRET</title></head><body><p>private@acme-widgets.co</p></body></html>"
