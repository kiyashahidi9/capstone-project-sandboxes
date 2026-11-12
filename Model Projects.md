Model projects are existing tools or projects that help your team define your own Capstone project.

Model projects act as an anchor for your team, as they give teams a base from which to:

understand the problem space
define the use case of the existing solution and its alternatives
develop a sense of reasonable technical benchmarks for the project in that problem domain (e.g. space, storage size, speed, feature set, etc)
spark excitement and curiosity around a given domain by making theoretical discussions concrete
give you hands-on experience with the problem domain and existing toolset, so that you can speak from a place of some experience and feel the pain of the problem, rather than it being totally theoretical
Model projects, and how they are utilized by Capstone teams, can vary widely. Several common patterns we've seen are:

Open source projects
Laridae is a Capstone project that facilitates reversible schema migrations in PostgreSQL with no downtime. Their model project is an open source tool called pgroll, which does exactly the same thing and was instrumental in helping the Laridae team understand the program space around schema migrations, how to make them reversible, and, from a technical perspective, what it would take to eliminate any downtime.
the Conifer project takes an open source testing application called Cypress, and creates tooling to make this application easy to deploy and horizontally scalable to allow users to parallelize end-to-end tests instead of executing them sequentially to reduce overall testing time.
Paid products
the Sundial project is an open-source implementation of Cronitor
A specific feature within a larger service or product
the Tinker and Snowclone projects are both Backends-as-a-Service (BaaS) that used Supabase as their model project
Tinker focused on implementing the Excel-like database UI dashboard that Supabase includes
Snowclone focused instead on the real-time notification features
A slightly different use case, or extension of a product
The Pinniped project is also a BaaS and used PocketBase as their model project. Pocket Base allows frontend developers to extend the out-of-the-box backend functionality with Go, with a limited ability to extend using JavaScript
Pinniped instead allows frontend developers to extend their backend with customized logic using the full breadth of JavaScript, since frontend developers will more likely be proficient (and already using) JavaScript
An architectural pattern to accomplish some result, or fix an existing issue
These tend to be discussed as part of engineering blog posts or youtube tech talks
For example, the Edamame project used an engineering blog post on an internal tool at Slack called Koi Pond as their model project

Both top-down and bottom-up research approaches result in finding model projects.

Top-down research drives toward a model project by recursing through increasingly specific subproblems and solutions. A model project represents the solution to a very specific subproblem.
Bottom-up research starts with a model project, and "fills in the story" by exploring alternative solutions to the subproblem, then working "up the tree" toward increasingly general subproblems.
The document Narrowing Down your Problem Space will illustrate this research process toward a model project in more detail.
