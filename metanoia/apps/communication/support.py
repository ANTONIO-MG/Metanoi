"""The Support / Help centre — a searchable user manual for the whole platform.

Content lives here as data (no database, no migration). Each article has an
``audience`` — one name or a tuple of names — so it is only shown to the people
it is for:

  * ``all``      — everyone
  * ``student``  — students
  * ``parent``   — parents / guardians / sponsors
  * ``educator`` — educators (coaches)
  * ``staff``    — admin **and** staff

An article for students and parents says ``('student', 'parent')``; a teaching
guide that admin/staff also need says ``('educator', 'staff')``.

The page (:func:`support`) filters the corpus by the viewer's role and by a
free-text search across the title, keywords and body, then groups what remains
by section. Add an article by appending an ``_a(...)`` to :data:`HELP_ARTICLES`
(ids must be unique — they are the page anchors, ``#help-<id>``).

Every menu label, button name and path below is checked against the templates
and URL confs. When a screen changes, change its article in the same commit.

:data:`HELP_ARTICLES` is ordered by section, following :data:`SECTION_ORDER`.
An article with an empty audience ``()`` is shown to nobody: the
``assessments`` and ``revision`` guides were retired for students together
with their pages (``core.student_hidden``) and are kept for history.

Module constants:

``AUDIENCE_LABEL``
    The viewer's audience → the label shown under the page title.
``SECTION_ORDER``
    The order the page renders its sections in.
``SECTION_ICONS``
    One Bootstrap icon per section, for the jump links at the top of the page.
``STUDENT_PARENT`` / ``TEACHING`` / ``LEARNERS_AND_TEAM``
    Common audience tuples; ``LEARNERS_AND_TEAM`` is everyone but parents.
"""
import re

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from core.roles import role_flags

AUDIENCE_LABEL = {
    'all': 'Everyone', 'student': 'Students', 'parent': 'Parents & guardians',
    'educator': 'Educators', 'staff': 'Admin & staff', 'community': 'Community members',
}

SECTION_ORDER = [
    'Getting started', 'Your account & settings', 'Messages & notifications',
    'Your studies', 'Live classes & the calendar', 'Payments, the shop & 1-on-1s',
    'For parents & guardians', 'Teaching', 'Administration', 'Help & troubleshooting',
]

SECTION_ICONS = {
    'Getting started': 'bi-compass',
    'Your account & settings': 'bi-person-gear',
    'Messages & notifications': 'bi-bell',
    'Your studies': 'bi-mortarboard',
    'Live classes & the calendar': 'bi-camera-video',
    'Payments, the shop & 1-on-1s': 'bi-bag',
    'For parents & guardians': 'bi-people',
    'Teaching': 'bi-easel',
    'Administration': 'bi-person-workspace',
    'Help & troubleshooting': 'bi-life-preserver',
}

STUDENT_PARENT = ('student', 'parent')
TEACHING = ('educator', 'staff')
LEARNERS_AND_TEAM = ('student', 'educator', 'staff')


def _a(id, title, section, audience, icon, keywords, body):
    """Build one help article; ``audience`` may be a single name or a tuple of names."""
    audiences = (audience,) if isinstance(audience, str) else tuple(audience)
    return {'id': id, 'title': title, 'section': section, 'audience': audiences,
            'icon': icon, 'keywords': keywords, 'body': body.strip()}


HELP_ARTICLES = [
    _a('top-bar', 'Finding your way around', 'Getting started', 'all', 'bi-compass',
       'navigation navbar top bar sidebar menu dashboard home search cart bell profile phone layout where',
       """
<p>Every page has the same two pieces: the <strong>top bar</strong> and the <strong>sidebar</strong> on the left.
On a phone, tap the menu icon (three lines) to open the sidebar.</p>
<h4>The top bar</h4>
<ul>
  <li><strong>Dashboard</strong> — your home page. Admin and staff land on the Staff desk instead, and educators on
      their teaching desk.</li>
  <li><strong>My Programme</strong> — the programme(s) you are registered for. Parents see their child's name here
      instead, which opens the child's progress.</li>
  <li>The <strong>search box</strong> — find people and lessons by name.</li>
  <li>The <strong>cart</strong>, <strong>Messages</strong> and <strong>Settings</strong> (gear) icons.</li>
  <li>The <strong>bell</strong> — your notifications and the messages addressed to you.</li>
  <li>Your <strong>picture</strong> — opens a menu with <strong>View profile</strong>,
      <strong>Settings &amp; privacy</strong>, the light/dark <strong>Mode</strong> buttons and the
      <strong>Language</strong> switch.</li>
</ul>
<h4>The sidebar</h4>
<p>The sidebar shows only what your role uses: students see <strong>My learning</strong> and
<strong>Account</strong>; parents see their child's pages; educators see <strong>Teaching</strong>; admin and staff
see <strong>Administration</strong> and <strong>Operations</strong>. <strong>Support &amp; help</strong> (this page) is
at the bottom, with quick links to your <strong>Profile</strong>, <strong>Settings</strong>,
<strong>Notifications</strong>, <strong>Terms</strong> and <strong>Privacy</strong>.</p>
<p>Wherever you see a person's picture and name, click the name to open their person card.</p>
"""),

    _a('signing-in', 'Signing in, signing up & resetting your password', 'Getting started', 'all', 'bi-box-arrow-in-right',
       'login log in sign in sign up register account password forgot reset google apple social email verify',
       """
<ul>
  <li><strong>Sign up</strong> with your e-mail address (we send a link to verify it) or with
      <strong>Google</strong> under “or continue with”. The Apple button shows “soon” until it is switched on.</li>
  <li>A new account goes straight to <strong>registration</strong>, where you choose your programme and modules.</li>
  <li>Forgot your password? On the log-in page, click <strong>Forgot password?</strong> and follow the link we e-mail you.</li>
  <li>Team accounts (admin, staff, educators) are created for you. The invitation e-mail has your log-in details;
      set your own password with <strong>Forgot password?</strong> and complete the short profile on first log-in.</li>
</ul>
"""),

    _a('student-start', 'Your sidebar, at a glance', 'Getting started', 'student', 'bi-signpost-split',
       'student sidebar my learning account menu modules sessions progress reports study groups finances',
       """
<p>At the top of the sidebar your picture shows three numbers: your <strong>Modules</strong>, how much of
<strong>Tests 1–4</strong> you have worked through, and your <strong>Readiness</strong>. Click them for your progress.</p>
<ul>
  <li><strong>Community</strong>, <strong>Messages</strong>, <strong>Notifications</strong>, <strong>Calendar</strong>.</li>
  <li><em>My learning</em>: <strong>My Modules</strong>, <strong>Online Sessions</strong>, <strong>My Progress</strong>,
      <strong>Reports</strong>, <strong>Study groups</strong>.</li>
  <li><em>Account</em>: <strong>My finances</strong>, <strong>Invoices</strong>, <strong>Statement</strong>,
      <strong>Payments</strong>, <strong>Shop</strong>, <strong>My cart</strong>, <strong>My orders</strong>.</li>
</ul>
<p>Your <strong>Dashboard</strong> (top bar) brings it together: what's coming up, your modules, live sessions,
notifications, messages, attendance and your achievements and certificates.</p>
"""),

    _a('register', 'Registering: programme, modules & prices', 'Getting started', 'student', 'bi-ui-checks',
       'register registration enrol enrolment institution programme cohort intake modules price fee monthly bundle discount all 4 milpark pgda p26s 750 2200 total',
       """
<p>Registration has three steps.</p>
<ol>
  <li><strong>Your details</strong> — your name, contact details and consent. You can also add a
      <strong>Parent / guardian / sponsor</strong> e-mail (optional); we invite them to an account linked to yours.</li>
  <li><strong>Choose your programme &amp; modules</strong> — pick your <strong>Institution</strong>, tick the programme,
      tick your <strong>cohort</strong> (intake), then tick the modules you are registering for. You are billed
      <strong>per module, per month</strong>, and the total updates as you tick.</li>
  <li><strong>Review &amp; finish</strong> — check the modules and the <strong>Total per month</strong>, then finish
      (see “Your free week”).</li>
</ol>
<h4>Fees &amp; the all-four price</h4>
<p>Every module is <strong>R750 per month</strong>, at every institution and in every programme. In a
programme of four modules (for example MACF, FREP, TAX and CGAU), tick all four and the
<strong>“All 4 modules”</strong> discount is applied for you, so the total is <strong>R2,200 a month</strong>. The
discount shows under the module list and on the review page — there is no code to enter.
One-on-one coaching is the only thing priced by the hour (R300 an hour).</p>
<p>Studying something that is not listed? See “One-on-one coaching for any programme”.</p>
"""),

    _a('one-on-one-request', 'One-on-one coaching for any programme', 'Getting started', 'student', 'bi-person-video3',
       'one-on-one 1-on-1 coaching request registration any institution programme not listed private tutor educator match',
       """
<p>On the programme step of registration, open the <strong>One-on-one coaching</strong> card. It is for any
institution and any programme, including ones not in our list.</p>
<ol>
  <li>Fill in your <strong>Institution</strong>, the <strong>Programme you are doing</strong>, a
      <strong>Start date</strong> and <strong>End date</strong>, and (optionally) the modules or subjects and anything we
      should know.</li>
  <li>Click <strong>Request one-on-one coaching</strong>. Nothing is charged now.</li>
  <li>Our team matches you with an educator and lets you know. Sessions are billed per hour once your educator is
      confirmed (the hourly rate is shown on the card).</li>
</ol>
"""),

    _a('terms', "Terms & Conditions and the “we've updated our Terms” prompt", 'Getting started', 'all', 'bi-file-earmark-text',
       'terms conditions service privacy popia refund policy shipping accept update prompt legal',
       """
<p>You accept the Terms &amp; Conditions when you register. When they change, a window titled
<strong>“We've updated our Terms &amp; Conditions”</strong> appears once on your next page. Click
<strong>Read the Terms</strong> to read them, then <strong>I accept</strong> to carry on.</p>
<p>You can read them at any time from <strong>Terms</strong> and <strong>Privacy</strong> at the bottom of the sidebar.
The Refund Policy and Shipping Policy are part of the Terms.</p>
"""),

    _a('profile', 'Your profile & profile picture', 'Your account & settings', 'all', 'bi-person-badge',
       'profile picture photo avatar name details change edit date of birth gender',
       """
<p>Open your profile from your picture in the sidebar or <strong>View profile</strong> in the top-bar menu.</p>
<ul>
  <li>To change your photo: <strong>Settings</strong> → <strong>Account &amp; profile</strong> →
      <strong>Profile picture</strong> → <strong>Save picture</strong>.</li>
  <li>Your name, gender and date of birth are shown there but can't be edited by you, because they are on your
      results and certificates. Ask an administrator to correct them.</li>
</ul>
"""),

    _a('settings', 'Settings: theme, language, time zone & privacy', 'Your account & settings', 'all', 'bi-sliders',
       'settings preferences privacy theme dark light mode match system language french portuguese english time zone timezone clock profile visibility show email activity allow messages direct chat',
       """
<p>Go to <a href="/community/settings/"><strong>Settings</strong></a> → <strong>Preferences &amp; privacy</strong>, change
what you need and click <strong>Save preferences</strong>. Theme, language and time zone apply straight away, on every
device you sign in from.</p>
<ul>
  <li><strong>Theme</strong> — Light, Dark or Match system. (The <strong>Mode</strong> buttons in your picture menu do
      the same thing.)</li>
  <li><strong>Language</strong> — English, French or Portuguese.</li>
  <li><strong>Time zone</strong> — session times, reminders and the calendar are all shown in this zone. Choose yours
      from the list (common zones first, then every other zone). Left on <em>Platform default</em>, times are shown in
      the platform's own zone.</li>
  <li><strong>Profile visibility</strong> — Public (anyone signed in), Members (people in your groups) or Private
      (only you).</li>
  <li><strong>Show email</strong> and <strong>Show activity</strong> — whether your profile shows your e-mail address
      and recent activity.</li>
  <li><strong>Allow messages</strong> — let other members start a direct chat with you. When it is off, other members
      can't start a new chat with you. Staff and educators can still reach you, and chats you already have stay open.</li>
</ul>
"""),

    _a('privacy-data', 'Your data, consent & closing your account', 'Your account & settings', 'all', 'bi-shield-lock',
       'popia privacy consent marketing research cookies data download export pdf json close delete account deactivate',
       """
<ul>
  <li><strong>Settings</strong> → <strong>Privacy &amp; consent</strong>: choose what you agree to (marketing e-mails,
      research, cookies and so on) and click <strong>Save privacy choices</strong>.</li>
  <li>On the same tab, <strong>Download my data (PDF)</strong> (or “or as JSON”) gives you a copy of everything we
      hold about you.</li>
  <li>To leave: <strong>Settings</strong> → <strong>Close account</strong> → <strong>Continue to close my account</strong>.
      You confirm with your password, your e-mail address or the word CLOSE. Your account is closed straight away and
      deleted after 30 days. Within those 30 days an administrator can still reactivate it.</li>
</ul>
"""),

    _a('two-step', 'Two-step sign-in (authenticator app)', 'Your account & settings', 'all', 'bi-key',
       'mfa 2fa two factor two-step authenticator app recovery codes security totp',
       """
<p>For extra security you can ask for a code from an authenticator app (such as Google or Microsoft
Authenticator) every time you sign in.</p>
<ol>
  <li>Open <a href="/accounts/2fa/">Two-Factor Authentication</a>.</li>
  <li>Under <strong>Authenticator App</strong>, click <strong>Activate</strong>, scan the code with your app and enter
      the six digits it shows.</li>
  <li>Set up <strong>Recovery Codes</strong> too and keep them somewhere safe. They get you in if you lose your phone.</li>
</ol>
"""),

    _a('the-bell', 'The bell: live alerts, sounds & desktop alerts', 'Messages & notifications', 'all', 'bi-bell',
       'bell notification sound chime alert badge dot unread real-time live mention desktop push browser',
       """
<p>The <strong>bell</strong> in the top bar is your live inbox. A red dot means something new is waiting. Open it to
see your newest <strong>notifications</strong> and the <strong>messages addressed to you</strong> (direct messages, and
group messages where someone @-mentioned you).</p>
<ul>
  <li>A soft chime plays when something arrives. Your browser may stay silent until you have clicked somewhere on the
      page once.</li>
  <li>Click a notification to read it in full; click a message to open the chat. Read items drop off the bell.</li>
  <li>The two buttons at the bottom open <strong>Notifications</strong> and <strong>Messages</strong>.</li>
  <li><strong>Desktop alerts</strong>: where your browser supports them, a <strong>Turn on</strong> button at the bottom
      of the bell lets alerts pop up on your computer even when Metanoia is closed.</li>
</ul>
"""),

    _a('notifications', 'Reading notifications: who sent it & what to do', 'Messages & notifications', 'all', 'bi-envelope-open',
       'notifications page letter from sender metanoia accounts learning hub info mailbox favourite star unread mark all read',
       """
<p><a href="/communication/notifications/"><strong>Notifications</strong></a> lists everything the platform has told you.
Click one to open it as a full-width letter: the subject, <strong>From</strong>, <strong>To</strong> (you), the date,
the message, a button to whatever it is about, and the sender's signature.</p>
<h4>Who it is from</h4>
<ul>
  <li>If a <strong>member of the team</strong> sent it, you see their picture, name and role. Click the name for their card.</li>
  <li>Otherwise it comes from the <strong>Metanoia mailbox</strong> it belongs to: <strong>Metanoia Accounts</strong> for money
      and your account, <strong>Metanoia Learning Hub</strong> for your studies (classes, new material, assessments,
      marks, the weekly summary) and <strong>Metanoia Info</strong> for everything else.</li>
</ul>
<h4>Actions</h4>
<ul>
  <li>Opening a notification marks it read. <strong>Mark as unread</strong> puts it back; <strong>Favourite</strong>
      keeps it in your Favourites list.</li>
  <li><strong>Mark all read</strong> on the list clears everything at once.</li>
</ul>
"""),

    _a('notification-prefs', 'Choosing what you are told — and what is e-mailed', 'Messages & notifications', 'all', 'bi-toggles',
       'notification preferences email copy reminders updates whatsapp desktop digest quiet hours unsubscribe mute categories',
       """
<p>Every notification always appears in the app. Reminders (upcoming live sessions, due dates) and updates (new
material, the weekly summary, class summaries) arrive in the app and as desktop alerts. By default they are
<strong>not</strong> e-mailed.</p>
<p>To change that, open <a href="/communication/notifications/preferences/"><strong>Notification preferences</strong></a>
(sidebar footer → <strong>Notifications</strong>, or <strong>Settings</strong> → <strong>Notifications</strong> →
<strong>Open</strong>):</p>
<ul>
  <li><strong>Channels</strong> — tick <strong>E-mail me messages from the Metanoia team</strong>,
      <strong>Reminders by e-mail</strong>, <strong>Updates by e-mail</strong>, <strong>Send me a copy on WhatsApp</strong>
      (needs a phone number on your profile) and desktop notifications.</li>
  <li><strong>Delivery</strong> — a digest, quiet hours and how early reminders arrive.</li>
  <li><strong>What to notify me about</strong> — switch whole kinds on or off (messages, deadlines, live sessions,
      grades, new material, assessments, the weekly summary…).</li>
</ul>
<p>Click <strong>Save preferences</strong>. Every automated e-mail also has an unsubscribe link at the bottom. Invoices,
receipts and account e-mails are always sent.</p>
"""),

    _a('messages', 'Messages, groups & connection requests', 'Messages & notifications', 'all', 'bi-chat-left-text',
       'chat message direct group module chat new chat connect connection request approve decline mention reply attach file photo video',
       """
<p><a href="/communication/chat/"><strong>Messages</strong></a> (parents: <strong>Message the school</strong>) holds all
your conversations.</p>
<ul>
  <li>Every module has its own <strong>group chat</strong>. You join it automatically once you have access to the
      module; it also appears on the module's <strong>Messages</strong> tab.</li>
  <li>Start a conversation with <strong>New chat</strong>; <strong>New group…</strong> makes a group of your own.</li>
  <li>Attach a document, photo or video, reply to a message, and type <strong>@</strong> to mention someone.</li>
</ul>
<h4>Messaging another student</h4>
<p>Students need an accepted connection before they can message each other directly. Open the person and click
<strong>Request to connect</strong>; they see <strong>Approve</strong> / <strong>Decline</strong>. This is only needed
between two students. Staff and educators can always message you. A member who has switched off <strong>Allow
messages</strong> in Settings can't be sent a new direct chat by other members.</p>
"""),

    _a('programme-feed', 'The Metanoia Community: posting, sharing, tagging & reporting', 'Messages & notifications',
       ('student', 'educator', 'staff', 'parent', 'community'), 'bi-people',
       'community programme feed social post share photo video document tag mention report like comment all metanoia',
       """
<p><strong>Community</strong> in the sidebar is the Metanoia Community — one place for students, coaches, parents, the
Metanoia team and community members.</p>
<ul>
  <li>The buttons across the top choose what you see: <strong>All</strong> (every community you belong to), the
      <strong>Metanoia Community</strong> (everyone), and the community of each programme you study, teach or follow.</li>
  <li>Write in <strong>“Share your thoughts…”</strong>, pick the community it goes to in the drop-down, and click
      <strong>Post</strong>. Everyone in that community can see it.</li>
  <li>Type <strong>@</strong> and the start of a name to <strong>tag</strong> someone in that community — they get a
      notification.</li>
  <li><strong>Like</strong>, <strong>Comment</strong>, or <strong>Share</strong> a post into another community you
      belong to, with a note of your own.</li>
  <li>See something that should not be there? Use the <strong>⋯</strong> menu → <strong>Report post</strong>. The
      Metanoia team reviews every report; a post reported by three people is hidden until they do.</li>
  <li>Click any picture, video or document to open it full screen.</li>
</ul>
"""),

    _a('community-member', 'You are a community member', 'Getting started', 'community', 'bi-stars',
       'community member join guest sidebar message metanoia shop invoice become student register limits',
       """
<p>You joined the <strong>Metanoia Community</strong>. Your sidebar has:</p>
<ul>
  <li><strong>Community</strong> — post, comment, like, share and tag people. Community members can post 5 times an
      hour (20 a day), comment 20 times an hour, and attach photos.</li>
  <li><strong>Notifications</strong> — announcements, tags and replies.</li>
  <li><strong>Message Metanoia</strong> — a private conversation with the Metanoia team. Anyone on the team can answer.</li>
  <li><strong>Shop</strong>, <strong>My cart</strong>, <strong>My orders</strong> and <strong>Invoices</strong> — buy
      study material and merchandise; every order gets an invoice.</li>
  <li><strong>Become a student</strong> — we e-mail you a link to confirm your address, you choose a new password, and
      then you register for your programme and modules like every other student.</li>
</ul>
"""),

    _a('study-groups', 'Study groups', 'Messages & notifications', LEARNERS_AND_TEAM, 'bi-people',
       'study groups workspace group files notes members classmates create edit close leave module share',
       """
<p><a href="/communication/study-groups/"><strong>Study groups</strong></a> are small groups for sharing files and notes
with classmates. (They used to be called Workspaces.)</p>
<h4>Starting a group</h4>
<ol>
  <li>Click <strong>New study group</strong>.</li>
  <li>Give it a <strong>Group name</strong>, choose a <strong>Module</strong> (only your own modules are offered) and say
      what the group is for.</li>
  <li>Under <strong>Members</strong>, search by name and tick people. Students can only add people on their own
      programme at the same institution; educators can add people on the programmes they teach.</li>
  <li>Save. Everyone you add gets a notification.</li>
</ol>
<h4>Inside a group</h4>
<ul>
  <li><strong>Shared files</strong> — upload notes or papers. Upload with the same title to add a new version.</li>
  <li><strong>Notes</strong> — write a note and pin the important ones.</li>
  <li>The person who made the group can change it with <strong>Edit group</strong>, or <strong>Close group</strong>;
      members can <strong>Leave group</strong>.</li>
  <li>A group tied to a module can also be opened by everyone on that module (listed under
      <strong>In your modules</strong>).</li>
</ul>
"""),

    _a('my-modules', 'My Modules and the module page', 'Your studies', 'student', 'bi-grid-1x2',
       'my modules module page tabs schedule messages documents live sessions mock exams blueprints assessments status active trial locked activity files',
       """
<p><a href="/learning/modules/"><strong>My Modules</strong></a> lists everything you registered for, each marked
<strong>Active</strong>, <strong>Trial · N days left</strong> or <strong>Locked</strong>. Click a module to open its page.</p>
<p>A module page has tabs: <strong>Schedule</strong>, <strong>Messages</strong> (the module's group chat),
<strong>Notifications</strong>, <strong>Documents</strong>, <strong>Live sessions</strong>, <strong>Mock exams</strong>,
<strong>Blueprints</strong> and <strong>Assessments</strong>.</p>
<p>The side panel shows the module's status and price, its coaches, an <strong>Activity</strong> list (when you joined,
your free week, invoices and payments, new material and recordings) and every file in the module.</p>
"""),

    _a('schedule', 'The schedule: blocks, weeks & materials', 'Your studies', 'student', 'bi-calendar3-week',
       'schedule block week topic study guide blueprint question pack solution answers mock order sequence hourglass padlock buy just this',
       """
<p>The <strong>Schedule</strong> tab is your study plan for your intake: preparation blocks (for example “Test 2
preparation”), the weeks inside each block, and the topics and materials inside each week — study guides, blueprints,
question packs, solutions, mocks, recordings and documents. A countdown shows the next block.</p>
<p>Work each week in order. Some items open only after an earlier step:</p>
<ul>
  <li>an <strong>hourglass</strong> means “not yet”: read the guide first, or submit your attempt to see the solution;</li>
  <li>a <strong>padlock</strong> means the module isn't paid for (see “Why is something locked?”).</li>
</ul>
<p>Some materials can be bought on their own: look for <strong>Buy just this</strong>. Study guides and blueprints open as
articles with a reading-progress bar, sections that tick off as you read, and bookmarks.</p>
"""),

    _a('lessons', 'Lessons: who is here & raising your hand', 'Your studies', 'student', 'bi-journal-richtext',
       'lesson read lesson player presence here now raise hand question notes bookmark complete',
       """
<p>Lessons open from the schedule. While you read, the panel on the right shows how many people are
<strong>here now</strong>, with their pictures.</p>
<p>Click the <strong>hand</strong> button to raise your hand. Everyone in the lesson sees it, and your educator knows you
have a question. Click it again to lower it.</p>
"""),

    _a('assessments', 'Assessments: quizzes, tests, mock exams & past papers', 'Your studies', (), 'bi-ui-checks',
       'assessment quiz test mock exam past paper sit start continue sit again submit timer result marks solution memo released scenario',
       """
<p><a href="/assessments/"><strong>Assessments</strong></a> lists every quiz, test and mock exam on your modules, grouped
by module. Use the pills to filter by type and the <strong>Module</strong> list to narrow it down.</p>
<ol>
  <li>Click <strong>Start</strong> (or <strong>Continue</strong> if you began earlier; <strong>Sit again</strong> if attempts
      remain). If there is a time limit, the timer shows the time remaining.</li>
  <li>Past papers and mock exams from your schedule can be sat here too: the <strong>Scenario</strong> stays open above
      the questions while you write your answers.</li>
  <li>Click <strong>Submit</strong> when you are done.</li>
</ol>
<h4>Results and solutions</h4>
<ul>
  <li>Multiple-choice and short answers are marked as soon as you submit. Written answers are marked by your educator;
      until then the result says <em>Awaiting marking</em>.</li>
  <li>The suggested solution appears <strong>beside each of your answers</strong> on the result page once it is
      released. For most papers that is as soon as you have submitted your own attempt. The list tells you which:
      <strong>Solution after you submit</strong> or <strong>Solution released</strong>.</li>
  <li>Open a past result with <strong>Result</strong>.</li>
</ul>
"""),

    _a('revision', 'Revision cards', 'Your studies', (), 'bi-arrow-repeat',
       'revision review cards spaced repetition missed wrong questions again hard good easy boxes streak',
       """
<p>Questions you got wrong or only partly right come back as cards in
<a href="/revision/"><strong>Revision</strong></a>, until you know them.</p>
<ol>
  <li>Click <strong>Start review</strong>.</li>
  <li>Answer the question (on paper for calculations), then <strong>Show answer</strong> or <strong>Check answer</strong>.</li>
  <li>Rate yourself: <strong>Again</strong>, <strong>Hard</strong>, <strong>Good</strong> or <strong>Easy</strong>. Cards
      you know come back less often (up to five weeks later); cards you missed come back tomorrow.</li>
</ol>
"""),

    _a('reports', 'Reports: results, report card & what you have completed', 'Your studies', 'student', 'bi-clipboard-data',
       'reports report card assessment report results grades what you have completed timeline history completion module block',
       """
<p><a href="/reports/"><strong>Reports</strong></a> has three parts:</p>
<ul>
  <li><strong>Assessment report</strong> — every quiz, test and mock exam you handed in, with the date, score and result.</li>
  <li><strong>Module report card</strong> — your weighted final mark and grade per module, once marked work comes in.</li>
  <li><strong>What you have completed</strong> — a timeline, filterable by <em>Blocks &amp; modules</em>,
      <em>Assessments</em>, <em>Classes</em> and <em>Lessons</em>.</li>
</ul>
<h4>What counts as completed</h4>
<ul>
  <li>An assessment — when you submit it.</li>
  <li>A live class — when you joined it from Metanoia, or the register marks you present or late.</li>
  <li>A lesson — when you finish it.</li>
  <li>A preparation block (Test 1–4, the exams) — when everything released to you in it is done.</li>
  <li>A module — when you pass it, or when its teaching is over and you have done everything in it.</li>
</ul>
"""),

    _a('progress', 'My Progress: how you are doing & what to improve', 'Your studies', 'student', 'bi-graph-up',
       'progress dashboard overall per module readiness what to improve going well activity results over time',
       """
<p><a href="/reports/progress/"><strong>My Progress</strong></a> is your progress dashboard:</p>
<ul>
  <li><strong>Overall progress</strong> — how much of the work released to you so far you have done (lessons, assessments
      and live classes).</li>
  <li><strong>What to improve</strong> — a short list of things to do next, each linking to where you do it — and what is
      <strong>Going well</strong>.</li>
  <li><strong>Readiness</strong>, your <strong>Activity</strong> per week and your <strong>Results over time</strong>.</li>
  <li><strong>Progress per module</strong> — a card for each module.</li>
</ul>
"""),

    _a('certificates', 'Certificates', 'Your studies', 'student', 'bi-patch-check',
       'certificate certificates award alumni download pdf verify achievements',
       """
<p>When a certificate is issued to you, it appears on your <strong>Dashboard</strong> in the
<strong>Achievements</strong> card. Click it to open the certificate, where you can download it. Each certificate has
its own verification link, so anyone you show it to can check it is genuine.</p>
"""),

    _a('online-sessions', 'Online Sessions: joining a class & watching recordings', 'Live classes & the calendar', 'student', 'bi-camera-video',
       'online sessions live class join teams upcoming past recording watch transcript summary attendance name 1-on-1',
       """
<p><a href="/communication/meetings/"><strong>Online Sessions</strong></a> has two views: <strong>Upcoming</strong> and
<strong>Past sessions</strong>.</p>
<ul>
  <li><strong>Upcoming</strong> lists the classes for your modules and intake, plus your 1-on-1 sessions. Click
      <strong>Join</strong>, or open the session and click <strong>Join the Teams session</strong>.</li>
  <li>Teams asks for your name: type it exactly as the session page shows, so your attendance is recorded.</li>
  <li><strong>Past sessions</strong> lists finished classes with their recordings. Recordings play here in the hub.</li>
</ul>
<p>You get a notification when a class is scheduled, 24 hours before it starts and 30 minutes before.</p>
"""),

    _a('after-class', 'After class: recordings & the session pack', 'Live classes & the calendar', LEARNERS_AND_TEAM, 'bi-stars',
       'after class recording transcript summary session pack next agenda action items onedrive message',
       """
<p>After each class, usually by the next morning, the recording and transcript are added to the session. You get
<strong>one message</strong> for the class with:</p>
<ul>
  <li>a written summary of what was covered,</li>
  <li>the agenda for the next session,</li>
  <li>your action items,</li>
  <li>links to the recording and the transcript.</li>
</ul>
<p>Open the session from <strong>Past sessions</strong> to watch it again or <strong>Read the summary</strong>.</p>
"""),

    _a('calendar', 'The calendar, reminders & adding it to your own calendar', 'Live classes & the calendar', 'all', 'bi-calendar-week',
       'calendar month list agenda day reminder remind me ics feed subscribe google outlook apple connect calendar time zone',
       """
<p><a href="/calendar/"><strong>Calendar</strong></a> shows live classes, academic dates (tests, exams, coaching nights),
assessment deadlines, new lessons and your own reminders. Students see only their own programme, modules and
intake; parents see their child's.</p>
<ul>
  <li>Switch between <strong>Month</strong> and <strong>List</strong>. Click an item for its details, or
      “+N more” to open the whole day.</li>
  <li><strong>Add a reminder</strong> for anything personal, then <strong>Remind me</strong>.</li>
  <li>Times are shown in <em>your</em> time zone (Settings → Preferences &amp; privacy).</li>
</ul>
<h4>See it in Google, Outlook or Apple Calendar</h4>
<p><strong>Settings</strong> → <strong>Connected calendars</strong>:</p>
<ul>
  <li><strong>Your Metanoia calendar, in your own app</strong> — copy your private calendar address and subscribe to it.
      Keep it private: anyone with the address can see your calendar. You can reset it if it leaks.</li>
  <li><strong>Bring your own calendar in</strong> — add your own calendar's address and its events show on Metanoia.</li>
</ul>
"""),

    _a('free-week', 'Your free week & finishing registration', 'Payments, the shop & 1-on-1s', 'student', 'bi-hourglass-split',
       'free week trial seven days 7 days pay now enrol eft invoice expired days left countdown registration finish',
       """
<p>On the last registration step there are two ways to finish:</p>
<ul>
  <li><strong>Pay now &amp; enrol</strong> — card or instant EFT through PayFast; your modules open as soon as payment
      clears. If the payment doesn't go through, you still get the free week.</li>
  <li><strong>Try 7 days, pay by EFT</strong> → <strong>Start my free week</strong> — everything opens for seven days and
      we e-mail your invoice so you can pay by EFT while you study.</li>
</ul>
<p>During the free week, <strong>My Modules</strong> shows <em>Trial · N days left</em> and each module page shows a
banner with an <strong>Unlock</strong> button. You get a reminder with the invoice the day before it ends. If it is
still unpaid when the week ends, the module closes (“Access expired”) until the payment is recorded. Nothing is lost —
pay any time to open it again.</p>
"""),

    _a('unlock-module', 'Paying for & unlocking a module', 'Payments, the shop & 1-on-1s', 'student', 'bi-unlock',
       'unlock locked module pay payment payfast eft invoice monthly price lock open',
       """
<p>On <strong>My Modules</strong> click <strong>Unlock</strong> on the module (or <strong>Unlock the module</strong> on
its page). Then choose:</p>
<ul>
  <li><strong>Pay now</strong> — card or instant EFT through PayFast; it unlocks straight away.</li>
  <li><strong>E-mail me an invoice to pay by EFT</strong> — the module unlocks as soon as your EFT is recorded.</li>
</ul>
<p>Free modules are never locked.</p>
"""),

    _a('paying-invoices', 'Invoices, paying & receipts', 'Payments, the shop & 1-on-1s', STUDENT_PARENT, 'bi-receipt',
       'invoice invoices pay payfast eft reference refund policy accept receipt statement payments history my finances fees',
       """
<ul>
  <li><strong>Invoices</strong> (parents: <strong>Fees &amp; invoices</strong>) lists every invoice. Open one to see it,
      download the <strong>Invoice PDF</strong>, or pay.</li>
  <li><strong>Pay with PayFast</strong> shows the amount and the refund policy. Tick the box to accept it, then
      <strong>Accept and pay</strong>. If the page says online payment isn't available, pay by EFT instead.</li>
  <li>To pay by EFT, use the invoice reference shown on the invoice (“Or pay by EFT using the reference …”). The
      invoice is marked paid once the payment is recorded.</li>
  <li>When an invoice is paid you get a receipt; <strong>Download receipt</strong> on the invoice gets it again.</li>
  <li><strong>Payments</strong> is your payment history, <strong>Statement</strong> your account statement, and
      <strong>My finances</strong> shows what you have paid month by month and what is still to come.</li>
</ul>
<p>Fees are final once paid, except where the law requires a refund. See the Refund Policy in the Terms.</p>
"""),

    _a('shop', 'The shop: study materials, merchandise & delivery', 'Payments, the shop & 1-on-1s', STUDENT_PARENT, 'bi-bag',
       'shop store buy study materials download merchandise hoodie colour size cart checkout coupon discount delivery courier pudo locker orders',
       """
<p>The <a href="/shop/"><strong>Shop</strong></a> sells study resources, 1-on-1 time with a coach and Metanoia gear.</p>
<ul>
  <li>Products with options (for example colour and size) show swatches and size buttons. Pick one, then add it to
      your cart.</li>
  <li><strong>My cart</strong> → <strong>Checkout</strong>. Enter a coupon code in <strong>Coupon code</strong> if you have
      one; automatic deals are applied for you. Prices include VAT.</li>
  <li><strong>Delivery</strong> for physical items: courier to your door (R120) or to a Pudo locker (R70). Delivery is
      free on orders from R1,500.</li>
  <li>Downloads are in <strong>My orders</strong>, for the account that bought them.</li>
</ul>
"""),

    _a('book-one-on-one', 'Booking a one-on-one session with a coach', 'Payments, the shop & 1-on-1s', 'student', 'bi-person-video3',
       'one-on-one 1-on-1 book booking coach educator hour slot calendar week red booked hold cart pay reschedule cancel refund credit r300 price',
       """
<p>In the <strong>Shop</strong>, the <strong>One-on-one sessions</strong> section lists coaches (educators and staff).
Search by name, or click <strong>See all … coaches</strong>.</p>
<ol>
  <li>Click <strong>See the week</strong> on a coach's card. Each coach shows their price per hour.</li>
  <li>The week shows one-hour slots <strong>in your own time zone</strong>. Booked hours are red; use the arrows for the
      next or previous week.</li>
  <li>Choose a free hour, say what you would like to cover (optional) and click <strong>Hold &amp; add to cart</strong>.
      The hour is held for you for 15 minutes.</li>
  <li>Pay from your cart. The Teams link arrives as soon as you have paid.</li>
</ol>
<h4>Your bookings</h4>
<p>Upcoming 1-on-1s are on <a href="/communication/meetings/"><strong>Online Sessions</strong></a>, with
<strong>Join</strong> and <strong>Reschedule</strong>. You can move a session up to 24 hours before it starts; after
that it shows “Too close to move”. Sessions are not refundable. If you cancel before the session starts, its value can
be added to your account as credit, less a cancellation charge. If we or the educator cancel, you choose a new time or
get the full value as credit. Credit is offered at checkout on your next purchase.</p>
"""),

    _a('parent-overview', 'What you can see and do as a parent', 'For parents & guardians', 'parent', 'bi-people',
       'parent guardian sponsor child progress results certificates assessments attendance message school fees invoices pay switch child',
       """
<p>Your home page (<strong>My children</strong> in the top bar) lists the students linked to you. If there is more
than one, choose which to <strong>follow</strong>: their progress, results and fees then open on that child
until you choose another (the name in the top bar switches too).</p>
<p>Your sidebar is built around the child you follow (their name is the heading):</p>
<ul>
  <li><strong>Progress</strong> / <strong>Progress &amp; analytics</strong> — their progress dashboard.</li>
  <li><strong>Results &amp; certificates</strong> — their assessment report, module report card and completed work.</li>
  <li><strong>Assessments</strong> — what has been set and how it went. You can follow results; sitting the
      assessments is up to your child.</li>
  <li><em>School</em>: the <strong>Community</strong> feed, <strong>Message Metanoia</strong>, <strong>Messages</strong>,
      <strong>Notifications</strong>, <strong>Calendar</strong>.</li>
  <li><em>Account</em>: <strong>Finances</strong>, <strong>Fees &amp; invoices</strong>, <strong>Statement</strong>,
      <strong>Payments</strong>, <strong>Shop</strong>, <strong>My Cart</strong>, <strong>My orders</strong>,
      <strong>My profile</strong>, <strong>Settings</strong>.</li>
</ul>
<p>You can view and pay your child's invoices; changes to enrolment go through the school.</p>
"""),

    _a('parent-link', 'Linking a parent or guardian', 'For parents & guardians', STUDENT_PARENT, 'bi-link-45deg',
       'parent guardian sponsor invite link account e-mail registration two parents',
       """
<p>A student adds a parent, guardian or sponsor on the first registration step: choose what they are to you and enter
their e-mail address under <strong>Parent / guardian / sponsor</strong> (not your own). As soon as you save that
step we e-mail them an invitation. The link opens one short page: they confirm their name and cell number, choose a
password, and their account is created already linked to yours. The link works once, for 30 days. A student can
have up to two linked parents.</p>
<p>Need to add or change a link later? Message Metanoia; the team can link a parent to a student for you.</p>
"""),

    _a('educator-start', 'Your teaching desk & sidebar', 'Teaching', 'educator', 'bi-easel',
       'educator teaching desk dashboard sidebar content assessment my classes insights nudge inactive marking',
       """
<p>After you sign in you land on <strong>My teaching desk</strong>: upcoming sessions, recent submissions, your
<strong>Marking queue</strong>, attendance at your last sessions and <strong>Students falling behind</strong> (with
<strong>Nudge inactive students</strong>).</p>
<p>The <em>Teaching</em> part of the sidebar groups everything else:</p>
<ul>
  <li><strong>Content</strong> — <strong>Manage lessons</strong>, <strong>New lesson</strong>.</li>
  <li><strong>Assessment</strong> — <strong>All assessments</strong>, <strong>Build assessments</strong>,
      <strong>New assessment</strong>, <strong>Marking queue</strong>.</li>
  <li><strong>My classes</strong> — <strong>Schedule a session</strong>,
      <strong>Past sessions</strong>, <strong>Live sessions</strong>, <strong>Discussions</strong>.</li>
  <li><strong>Insights</strong> — <strong>Reports</strong>, <strong>Analytics</strong>.</li>
  <li><strong>Study groups</strong>.</li>
</ul>
<p>You see the modules you teach. Your <strong>Email</strong> (Metanoia Mail) is in the top part of the sidebar.</p>
"""),

    _a('build-schedule', 'Building a module schedule', 'Teaching', TEACHING, 'bi-tools',
       'build schedule scaffold block week material upload publish release free preview sell shop intake cohort',
       """
<p>Open the module and click <strong>Build schedule</strong> in its side panel.</p>
<ol>
  <li>No schedule yet? <strong>Scaffold the year</strong> creates the standard blocks and weeks. Or use <strong>Add a
      preparation block</strong> and <strong>Add a week</strong>.</li>
  <li><strong>Add material</strong> to a block or week: <strong>Upload a file</strong>, or link a lesson, a link, a live
      session or an assessment. Set its kind (study guide, blueprint, question pack, solution, mock…).</li>
  <li>Options: <strong>Release at</strong> a date, <strong>Free preview (open even when locked)</strong>, or <strong>Also
      sell this on its own in the shop</strong> with a price.</li>
  <li>Tick <strong>Visible to students</strong> to publish it. Drafts stay hidden.</li>
</ol>
<p>Use <strong>Switch intake</strong> to work on one intake, or the <strong>Shared template — every cohort</strong>. The
teaching order is applied automatically: a solution only opens for a student after they submit their attempt.</p>
"""),

    _a('lesson-creator', 'Writing lessons', 'Teaching', TEACHING, 'bi-journal-richtext',
       'lesson creator new lesson editor blocks text image video quiz live session publish ai build',
       """
<ol>
  <li>Sidebar → <strong>Content</strong> → <strong>New lesson</strong> (or <strong>Manage lessons</strong> →
      <strong>New lesson</strong>).</li>
  <li>Enter the <strong>Lesson title</strong>, the <strong>Module</strong> and the estimated study time, then
      <strong>Create &amp; edit</strong>.</li>
  <li>Build the lesson from blocks: headings, text, images, audio and video, tables, callouts, quizzes and live sessions.</li>
  <li>Publish it when it is ready.</li>
</ol>
<p><strong>Build with AI</strong> on the Lesson creator page drafts a lesson for you to review.</p>
"""),

    _a('build-assessments', 'Building assessments', 'Teaching', TEACHING, 'bi-pencil-square',
       'build assessment new assessment questions time limit attempts pass mark publish ai from pdf memo solution release',
       """
<ol>
  <li>Sidebar → <strong>Assessment</strong> → <strong>New assessment</strong>. Choose the type and module, the time
      limit (0 = no limit), the number of attempts (0 = unlimited) and the pass percentage, then
      <strong>Create &amp; add questions</strong>.</li>
  <li>Add questions in the builder. Objective questions are marked automatically; written questions need a rubric or
      model answer and go to the marking queue.</li>
  <li>Publish it when it is ready. <strong>AI from PDF</strong> on the assessments list can draft one from a paper and
      its memo.</li>
</ol>
<p>Past papers and mock exams on the module schedule also appear under <strong>All assessments</strong> as papers
students can sit, with the suggested solution shown once it is released. Use <strong>Preview</strong> to see one as a
student would.</p>
"""),

    _a('marking', 'Marking written answers', 'Teaching', TEACHING, 'bi-clipboard-check',
       'marking queue mark rubric feedback score save marks suggested ai attempt',
       """
<ol>
  <li>Open <a href="/assessments/marking/"><strong>Marking queue</strong></a>. It lists submitted attempts with answers
      waiting for a mark.</li>
  <li>Click <strong>Mark</strong>. Each answer shows the student's answer next to the rubric or model answer.</li>
  <li>Tick the marking points made (they add up into the marks box), or type the marks, and add feedback. A suggested
      mark may be offered; it is only advice and you set the mark (<strong>Use this mark</strong>).</li>
  <li><strong>Save marks</strong>. The student sees the result and your feedback.</li>
</ol>
"""),

    _a('schedule-session', 'Scheduling a live session', 'Teaching', TEACHING, 'bi-calendar-plus',
       'schedule session live class teams meeting audience module cohort programme invited private reminders edit cancel attendance register',
       """
<ol>
  <li>Sidebar → <strong>Schedule a session</strong> (or <strong>Schedule a session</strong> on the Calendar).</li>
  <li>Choose who it is for: <strong>One module</strong> (optionally one intake), <strong>One cohort</strong>,
      <strong>One programme</strong> or <strong>Invited people only (incl. one-on-one)</strong>. Admin and staff can
      also choose one institution or everyone. Educators can only schedule for the modules they teach.</li>
  <li>Set the date and time and save. The session is created in Microsoft Teams and records automatically.</li>
</ol>
<p>Only the people in that audience are told about it and see it. They get a notification when it is scheduled,
24 hours before and 30 minutes before. Open the session to <strong>Edit</strong>, <strong>Cancel</strong> or copy the
joining link.</p>
<p><strong>Attendance</strong> is recorded automatically: from the time students spend in the session on Metanoia
and from the Teams attendance report once the class ends. Open the session from <strong>Past sessions</strong>
to see its register.</p>
"""),

    _a('coach-one-on-ones', 'Your 1-on-1 sessions as a coach', 'Teaching', TEACHING, 'bi-person-video3',
       'one-on-one 1-on-1 coach educator booked with me hours availability calendar cancel credit reschedule',
       """
<ul>
  <li>Students book you from the shop's <strong>One-on-one sessions</strong>. Your own page there shows your booking
      calendar as students see it; red hours are booked with you.</li>
  <li>Your bookings are on <strong>Online sessions</strong> under <strong>1-on-1 sessions</strong>:
      <strong>Booked with me</strong> shows each student and what they want to cover, with <strong>Join</strong>.</li>
  <li>If you can't make it, <strong>Cancel &amp; credit</strong> cancels the session and gives the student its value as
      account credit.</li>
  <li>Your weekly hours, time off and hourly rate are set by staff under <strong>Shop</strong> →
      <strong>Educators &amp; hours</strong>. Ask them to change your hours.</li>
</ul>
"""),

    _a('student-view', 'Student view: seeing what a student sees', 'Teaching', TEACHING, 'bi-eye',
       'student view preview switch see as student exit change what i see',
       """
<p>Click <strong>Student view</strong> in the top bar and choose an institution or programme. Educators can choose
what they teach; admin and staff can choose any. You then see the hub as a student of that programme does. Chats are
read-only while you are in student view.</p>
<p>Use the <strong>Student view</strong> menu to <strong>Change what I see</strong> or <strong>Exit student view</strong>.</p>
"""),

    _a('metanoia-mail', 'Metanoia Mail: your mailbox, long recipient lists & signatures', 'Teaching', TEACHING, 'bi-envelope',
       'metanoia mail email mailbox inbox compose new mail to cc bcc paste list recipients invalid more batch send drafts signature',
       """
<p><strong>Email</strong> in the sidebar opens Metanoia Mail, your mailbox on the Metanoia domain (your own address, and the
shared info@, accounts@ and learninghub@ boxes if you have them). New mail shows a badge and an alert.</p>
<h4>Writing to many people</h4>
<ul>
  <li>Click <strong>New mail</strong>. Open <strong>Cc &amp; Bcc</strong> if you need them.</li>
  <li><strong>Paste a whole list</strong> of addresses into To, Cc or Bcc and every address is added at once.</li>
  <li>A long list folds into a few names and a <strong>+N more</strong> button; click it to see them all. Every address is
      still sent.</li>
  <li>Badly typed addresses are highlighted; <strong>Remove N invalid</strong> deletes them in one click.</li>
  <li>Large lists are sent in batches for you, so the mail server accepts them. Use Bcc for class lists.</li>
</ul>
<h4>Signatures and drafts</h4>
<p><strong>Mail settings</strong> holds your Metanoia signature (<strong>Use the Metanoia signature</strong>) and
<strong>Copy for other mail apps</strong>. <strong>Send drafts</strong> sends drafts you have queued, one by one.</p>
"""),

    _a('staff-start', 'The staff desk & your sidebar', 'Administration', 'staff', 'bi-person-workspace',
       'staff desk operations dashboard student 360 sidebar administration operations menu',
       """
<p>Admin and staff land on the <strong>Operations dashboard</strong> (<a href="/staff/">Staff desk</a>).</p>
<ul>
  <li><em>Administration</em>: <strong>Members &amp; enrolment</strong>, <strong>Create team account</strong>
      (superusers), <strong>Closed accounts</strong>, <strong>Programmes</strong> (institutions, modules, online sessions,
      live session settings, YouTube recordings) and <strong>Staff desk</strong>.</li>
  <li><strong>Staff desk</strong>: Operations dashboard, <strong>Student 360</strong>, Educator desks, At-risk students,
      Grades &amp; certificates, AI reports, Invitations, Parent links, <strong>1-on-1 requests</strong>,
      Audit log, Background jobs and (admins) Backups &amp; restore.</li>
  <li><em>Operations</em>: Analytics, Error log, Learning Hub, Shop, Finance, Study
      groups, Assessments, Reports, Moderation, <strong>Notifications centre</strong>, WhatsApp.</li>
</ul>
<p><strong>Student 360</strong> puts everything about one student on one page: enrolments, marks, money and messages.</p>
"""),

    _a('members', 'Members, enrolment, team accounts & closed accounts', 'Administration', 'staff', 'bi-person-plus',
       'members enrolment enrol modules team account create staff educator admin invite closed accounts reactivate restore',
       """
<ul>
  <li><strong>Members &amp; enrolment</strong> — open a member to view their profile, or <strong>Enrol</strong> to tick
      the modules they hold and <strong>Save enrolment</strong>. Newly ticked modules start locked; they open on a free
      week or when their invoice is paid. <strong>Invite educator</strong> sends an educator a sign-up link.</li>
  <li><strong>Create team account</strong> (superusers) — an admin, staff or educator account with a verified e-mail and a
      random password. <strong>Create &amp; send invitation</strong> e-mails the log-in details.</li>
  <li><strong>Closed accounts</strong> — <strong>Reactivate</strong> an account within its 30 days; after that it is
      archived and can be <strong>Restore</strong>d.</li>
  <li><strong>Staff desk</strong> → <strong>Invitations</strong> and <strong>Parent links</strong> — manage sign-up
      links, and <strong>Link a parent to a student</strong> by e-mail.</li>
</ul>
"""),

    _a('one-on-one-requests', '1-on-1 requests from registration', 'Administration', 'staff', 'bi-person-video3',
       'one-on-one 1-on-1 requests registration assign educator enrolled decline admin notes coaching',
       """
<p>Requests made with the <strong>One-on-one coaching</strong> card at registration land in
<strong>Staff desk</strong> → <a href="/staff/one-on-one/"><strong>1-on-1 requests</strong></a> (the team is notified
too).</p>
<ol>
  <li>Use the tabs <strong>Open</strong>, <strong>Enrolled</strong>, <strong>Declined</strong> and <strong>All</strong>, or
      search.</li>
  <li>Each row shows the student, what and where they are studying and their dates.</li>
  <li><strong>Assign an educator…</strong>, add admin notes and click <strong>Save</strong>.</li>
  <li>When they are set up, click <strong>Mark enrolled</strong> — or <strong>Decline</strong>.</li>
</ol>
"""),

    _a('coach-hours', 'Coaches, hours & 1-on-1 bookings', 'Administration', 'staff', 'bi-clock',
       'educators hours availability time off rate services one-on-one bookings all cancel credit reschedule',
       """
<ul>
  <li><strong>Shop</strong> → <strong>Educators &amp; hours</strong> → <strong>Set hours</strong> on a person: add weekly
      windows (several per day are fine), <strong>Add time off</strong>, and set their services and rates. Then
      <strong>Save hours</strong>. Someone with no hours offers no slots.</li>
  <li>Every booking is on <strong>Online sessions</strong> → <strong>1-on-1 sessions</strong> → <strong>All</strong>. Staff
      can <strong>Reschedule</strong> at any time and <strong>Cancel &amp; credit</strong> (the student gets the value as
      account credit).</li>
</ul>
"""),

    _a('academic-structure', 'Institutions, programmes, modules & cohorts', 'Administration', 'staff', 'bi-diagram-3',
       'academic structure institution programme module cohort intake price add edit logo manage',
       """
<p><strong>Programmes</strong> → <strong>Institutions &amp; programmes</strong> (also <strong>Learning Hub</strong> →
<strong>Academic structure</strong>):</p>
<ul>
  <li><strong>Add institution</strong>, then open it to add its <strong>programmes</strong>.</li>
  <li>Each programme has its <strong>modules</strong> (each with a monthly price) and its <strong>cohorts</strong> (intakes).</li>
  <li>Upload a sharp institution logo; one that is too small to show clearly is replaced by a lettered badge.</li>
</ul>
<p>Bundles such as “All 4 modules” are automatic discounts under <strong>Shop</strong> → <strong>Discounts</strong>.</p>
"""),

    _a('content-pipeline', 'Uploading content', 'Administration', 'staff', 'bi-cloud-upload',
       'content queue upload create with ai format samples import pack json approve publish draft',
       """
<ul>
  <li><strong>Learning Hub</strong> → <strong>Upload content</strong> or <strong>Create with AI</strong>. Every draft waits
      in the <strong>Content queue</strong> until you approve it (it joins the module schedule, visible to staff only) and
      then publish it to the intake.</li>
  <li><strong>Upload format &amp; samples</strong> explains how to lay out a pack; <strong>Import content pack
      (JSON)</strong> takes a ready-made pack.</li>
</ul>
"""),

    _a('finance-admin', 'Invoices, payments & finance reports', 'Administration', 'staff', 'bi-receipt',
       'finance invoices new invoice bulk invoice record payment grant access estimates expenses items reports overdue reminder void payfast eft',
       """
<p>Under <strong>Finance</strong> in the sidebar:</p>
<ul>
  <li><strong>Invoices</strong> — <strong>New invoice</strong>, search, and open an invoice to <strong>Record a
      payment</strong>, <strong>Send reminder</strong>, <strong>Email from Metanoia Mail</strong>,
      <strong>Duplicate</strong> or <strong>Void this invoice</strong>. Paying a module invoice unlocks its modules.</li>
  <li><strong>Bulk invoice</strong> — invoice many people at once.</li>
  <li><strong>Payments</strong> — every payment received.
      <a href="/finance/payments/grant-access/"><strong>Record a payment &amp; grant access</strong></a> records an EFT
      and reopens a student's modules in one step; it also lists who is currently locked out.</li>
  <li><strong>Estimates</strong>, <strong>Expenses</strong> and <strong>Products &amp; services</strong> (the items
      catalogue).</li>
  <li><strong>Dashboard &amp; reports</strong> — income, outstanding balances and the reports.</li>
</ul>
<p>When online card payment is switched off, the pay page tells payers to arrange payment and EFT still works; record
those payments here.</p>
"""),

    _a('shop-admin', 'Running the shop: products, orders, delivery & discounts', 'Administration', 'staff', 'bi-shop',
       'shop products services add edit variants colour size orders fulfilment courier waybill packing slip shipped delivered discounts coupons automatic bundle',
       """
<ul>
  <li><strong>Shop</strong> → <strong>Products &amp; services</strong> — add and edit items. Options named like
      “Colour / Size” show as colour swatches and size buttons.</li>
  <li><strong>Orders</strong> and <strong>Fulfilment</strong> — pack an order, print the <strong>Packing slip</strong>,
      <strong>Book courier</strong> (or enter the waybill), then <strong>Mark shipped</strong> and
      <strong>Mark delivered</strong>.</li>
  <li><strong>Discounts</strong> — coupons and automatic deals, including bundles that apply when every named item is
      bought together.</li>
</ul>
"""),

    _a('notifications-centre', 'Sending notifications (Notifications centre)', 'Administration', 'staff', 'bi-megaphone',
       'notifications centre announcement broadcast send notification audience everyone choose who roles institution programme cohort module people parents schedule draft email important video files read rate',
       """
<ol>
  <li>Sidebar → <strong>Notifications centre</strong> → <strong>Send a notification</strong>.</li>
  <li>Write a <strong>Headline</strong> and <strong>Message</strong>. Optionally add a <strong>Link</strong> (with
      <strong>Link button text</strong>), a <strong>Video link</strong> (YouTube and Vimeo play inside the notification)
      and pictures, videos or documents.</li>
  <li><strong>Who gets it</strong>: <strong>Everyone</strong>, or <strong>Choose who</strong> — any mix of institutions,
      programmes, cohorts, modules and named people. Narrow it with <strong>Only these roles</strong>, and tick
      <strong>Copy parents / guardians</strong> if they should get it too. The page shows how many people it
      <strong>Will reach</strong>.</li>
  <li>Tick <strong>Also send by e-mail</strong> if needed, and <strong>Important</strong> to reach people who muted
      announcements.</li>
  <li><strong>Send now</strong>, <strong>Schedule for later</strong> or <strong>Save as draft</strong>.</li>
</ol>
<p>Notifications you send show your name and picture as the sender. The centre tracks each one's reach and read rate.</p>
"""),

    _a('grades-certs', 'Grades, certificates & at-risk students', 'Administration', 'staff', 'bi-award',
       'grades weightings recompute override certificates issue revoke reinstate regenerate at-risk risk scan snapshots triage notify',
       """
<ul>
  <li><strong>Grades &amp; certificates</strong> — weighted module marks. Open a grade to recompute it or override it
      with a reason; set component weights under <strong>Weighting</strong>.</li>
  <li><strong>Issue certificate</strong>, and revoke, reinstate or regenerate one from the certificates list. A revoked
      certificate's verify page says it was withdrawn.</li>
  <li><strong>At-risk students</strong> — <strong>Run risk scan</strong>, work through open flags (needs attention,
      acknowledged, resolved) and notify the student or parent. <strong>Snapshots</strong> keep a dated record.</li>
</ul>
"""),

    _a('whatsapp', 'WhatsApp inbox', 'Administration', 'staff', 'bi-whatsapp',
       'whatsapp inbox messages reply number verification templates meta',
       """
<p>Sidebar → <strong>WhatsApp</strong> is the shared WhatsApp inbox: conversations, who each number belongs to, and
replies. When WhatsApp is not connected yet, the page says so: replies are saved but not sent, and nothing arrives,
until the Meta set-up is finished.</p>
"""),

    _a('sessions-admin', 'Live session settings & YouTube backup', 'Administration', 'staff', 'bi-youtube',
       'live session settings teams youtube backup recordings playlists nightly onedrive',
       """
<ul>
  <li><strong>Programmes</strong> → <strong>Live session settings</strong> shows whether Microsoft Teams and YouTube are
      connected.</li>
  <li>Recordings play in the hub from OneDrive. YouTube is a <strong>backup only</strong>: each night a few new recordings
      are uploaded, into one playlist per institution and programme.</li>
  <li><strong>YouTube recordings</strong> lets you map imported playlists to a module and intake.</li>
</ul>
"""),

    _a('platform-ops', 'Backups, background jobs, error log, audit & moderation', 'Administration', 'staff', 'bi-hdd-stack',
       'backups restore back up now background jobs run all due error log diagnostics audit log export moderation penalties appeals',
       """
<ul>
  <li><strong>Backups &amp; restore</strong> (admins) — <strong>Back up now</strong>, download, upload, or restore a
      restore point (your password is needed). A safety backup is taken before every restore. A warning shows if there
      has been no automatic backup in the last 26 hours.</li>
  <li><strong>Background jobs</strong> — what runs unattended, when it last ran and whether it failed;
      <strong>Run all due jobs</strong>.</li>
  <li><strong>Error log</strong> — every fault, recorded once with a count, and a dictionary of error codes.</li>
  <li><strong>Audit log</strong> — sign-ins and recorded changes, with <strong>Export CSV</strong>.</li>
  <li><strong>Moderation</strong> — flagged content, penalties and appeals.</li>
</ul>
"""),

    _a('django-admin', 'The Advanced admin', 'Administration', 'staff', 'bi-shield-lock',
       'advanced admin django admin tables database find a table all apps drawer models records',
       """
<p><strong>Advanced admin</strong> (sidebar, opens in a new tab; formerly “Django Admin”) is the raw database editor for admin users. It now looks
like the rest of Metanoia. Type in <strong>Find a table…</strong> to jump to any table, or open <strong>All apps</strong>
for the full menu. Use it with care: changes take effect immediately. Deleting a user or person there archives the account for 5 days (hidden from the LMS, restorable from <strong>Closed accounts</strong>) before it is permanently deleted.</p>
"""),

    _a('locked', 'Why is something locked?', 'Help & troubleshooting', 'all', 'bi-lock',
       'locked lock cant open access module week solution mock paid hourglass padlock expired',
       """
<ul>
  <li><strong>A padlock</strong> — the module isn't paid for, or your free week has ended (“Access expired”). Click
      <strong>Unlock</strong> to pay.</li>
  <li><strong>An hourglass</strong> — you have the module but haven't reached this step yet: read the guide first, or submit
      your attempt to see the solution. It's a teaching step, not a payment.</li>
  <li>Some items are released on a set date and appear on that day.</li>
</ul>
"""),

    _a('times-wrong', 'Times look wrong', 'Help & troubleshooting', 'all', 'bi-clock-history',
       'time wrong hour off time zone timezone session time calendar clock',
       """
<p>All times are shown in the time zone on your account. Go to <strong>Settings</strong> → <strong>Preferences &amp;
privacy</strong>, choose your <strong>Time zone</strong> and click <strong>Save preferences</strong>. Session times,
reminders, the calendar and 1-on-1 slots all follow it.</p>
"""),

    _a('errors', 'Something went wrong — reading an error', 'Help & troubleshooting', 'all', 'bi-bug',
       'error code problem fix bug broken message support reference',
       """
<p>Most errors show a short <strong>code</strong> like <code>USER-1002</code> or <code>FIN-8002</code>. The first part
says which area it is (USER = accounts, FIN = payments, LRN = lessons and modules, and so on) and the message tells you
what to do. If it keeps happening, send the code to the team; it points them straight to the cause.</p>
"""),

    _a('contact', 'Who to contact for what', 'Help & troubleshooting', 'all', 'bi-headset',
       'contact support help who academic technical educator staff admin email whatsapp',
       """
<ul>
  <li><strong>Your studies</strong> — a topic, a mark, a lesson, the schedule: message your educator, or post in the
      module's group chat.</li>
  <li><strong>Payments, your account or the platform</strong> — message the team from <strong>Messages</strong>, or
      e-mail info@metanoiagroup.co.za.</li>
</ul>
"""),

    _a('help-centre', 'Using this help centre', 'Help & troubleshooting', 'all', 'bi-life-preserver',
       'help support search guide manual how to find article this page',
       """
<p>Use the <strong>search box</strong> at the top to find a topic by name or keyword, or jump to a section. The articles
are chosen for your role: students, parents, educators and admin/staff each see their own guides plus the general
ones.</p>
"""),
]


_TAG = re.compile(r'<[^>]+>')


def _plain(html):
    """Strip HTML tags from ``html`` so the search matches the visible text only."""
    return _TAG.sub(' ', html)


def _audience_for(request):
    """Return the help audience for the viewer's role (``staff``, ``educator``, ``parent``, ``community`` or ``student``)."""
    flags = role_flags(request)
    if flags.get('is_admin_staff'):
        return 'staff'
    if flags.get('is_educator'):
        return 'educator'
    if flags.get('is_parent'):
        return 'parent'
    if flags.get('is_community'):
        return 'community'
    return 'student'


def articles_for(audience, query=''):
    """The articles ``audience`` sees, filtered by ``query`` (title, keywords, body)."""
    visible = [a for a in HELP_ARTICLES if 'all' in a['audience'] or audience in a['audience']]
    if query:
        needle = query.lower()
        visible = [a for a in visible
                   if needle in a['title'].lower()
                   or needle in a['keywords'].lower()
                   or needle in _plain(a['body']).lower()]
    return visible


@login_required
def support(request):
    """The searchable, role-scoped help centre.

    Articles are grouped into sections in :data:`SECTION_ORDER`; a search with
    three results or fewer opens its articles straight away (``expand_all``).
    """
    query = (request.GET.get('q') or '').strip()
    audience = _audience_for(request)
    visible = articles_for(audience, query)

    by_section = {}
    for article in visible:
        by_section.setdefault(article['section'], []).append(article)
    sections = [{'name': name, 'slug': re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-'),
                 'icon': SECTION_ICONS.get(name, 'bi-dot'), 'articles': by_section[name]}
                for name in SECTION_ORDER if name in by_section]

    return render(request, 'communication/support.html', {
        'page_title': 'Support & help', 'query': query, 'sections': sections,
        'total': len(visible), 'audience_label': AUDIENCE_LABEL.get(audience, ''),
        'expand_all': bool(query) and len(visible) <= 3,
    })
