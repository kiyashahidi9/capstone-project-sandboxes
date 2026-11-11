We will demonstrate two approaches to understanding a specific domain and its engineering challenges: top-down and bottom-up.

Broadly defined:

With top-down research, you begin with a general engineering domain and investigate investigating increasingly specific "sub-problems" and solutions within that domain.
With bottom-up research, you begin by looking at a specific project or product, and investigate increasingly the general sub-problems that would lead an engineering team to that particular project.
Top-down research example:
Begin with the general topic of microservice communication
Identify the characteristics of microservice communication, and its various challenges (sub-problems)
Identify possible solutions for one of those subproblems, and evaluate the tradeoffs between those solutions. For example, message queues.
Define broadly what message queues are, when they are typically used
Identify a 'sub-problem' (challenge) in the message queue space, for example, unprocessed messages flooding the queue
Define the problem, how it is typically encountered, and the general solutions
Identify the various solutions to that subproblem, e.g. dead letter queues and replaying "dead letters"
Define the process of replaying "dead letters", who encounters this problem, the challenges encountered when using DLQs
Identify the tradeoffs between the various solutions....

See the first 1/3 of the Kuri writeup to see this process in action: https://kuri-dlq.github.io/

Conducting top-down research
We want each team to practice conducting top-down research.
Start with a general domain
Ask the following questions:
What is this?
Why and under what circumstances would I use this? (e.g. what types of applications or architectures are the use case? which aren't?)
What are the alternatives?
What new challenges does this tool/pattern/architecture create?
Find the challenges (subproblems) within the domain
Pick one specific subproblem and repeat the process:
Who experiences that subproblem and why?
What are the various "solutions" to that subproblem?
What are the differences between those solutions? What are the tradeoffs between them?
Repeat the process: Pick a specific solution within that subproblem space.
What new challenges does it create?
E.g. added architectural complexity? more setup time? vendor lock-in?
What solutions exist for those challenges? What are the tradeoffs between them?
Continue until you get as specific as possible. This likely means exploring a tool, or set of tools that solve a specific subproblem. You should be able to describe the characteristics of an application that would benefit from that tool, along with its limitations and alternatives.
How do I identify subproblems?
If there are multiple products within a given space, what are the differences between them?
If a single product has a lot of different features, then often each feature is a single subproblem that is experienced by a subset of an audience
Helpful resources in identifying subproblems if they arent clear: developer forums, reddit, and chatGPT
