As AI becomes more and more prominent tool in our lives, and especially in our technical work, we wanted to address how to use generative AI effectively during Capstone.

Definition

First of all, what do we mean by generative AI? In this context, we're referring to models and tools to assist you in the research and implementation of your projects. This includes general purpose tools like ChatGPT or Claude as well as AI-enhanced IDEs like Cursor.

Summary

The overall idea of Capstone is to help you get a job and prepare you for your first role as a software engineer. AI can help you achieve both of these goals if used in the right way, or it can hinder your progress if misused. Priority should be on learning and understanding, not using AI to sidestep difficult parts of the process or to prioritize velocity or generally "getting things done."

To put in another way, using generative AI in Capstone is like having a compass on a long hike through the wilderness of learning software engineering. The AI can point you in the right direction, suggest alternate routes, and help you avoid getting lost, but you still need to walk the path, understand the terrain, and make the key decisions. If you rely solely on the compass without paying attention to your surroundings, you'll fail to build the skills needed for the job hunt and your career. It's the journey, and the knowledge you gain along the way, that matters most.

Best Practices

Here are some best practices for using generative AI in Capstone.

Research - one of the best ways to use AI in Capstone is to help with your Capstone project research.
Here are some ways to use AI in research
summarize complex concepts and topics
discuss trade-offs and alternatives
think of additional ideas beyond the ones you've come up with so far
give you real-world examples to help clarify concepts you might be grappling with
generate some example code to show you how something might be implemented in a system. The point here is not to turn around and use that code, but use it to further your understanding of how theoretical concepts get applied
Example prompts
Can you summarize how event-driven architectures work in microservices, specifically how they help achieve loose coupling between services?
What are the trade-offs between using WebSockets and HTTP for real-time communication in a chat application?
What are some real-world use cases of GraphQL and why did certain companies choose it over REST APIs?
Can you show me how to implement a simple Pub/Sub system using JavaScript for real-time notifications?
Debugging - while it's a usually a good idea to take a stab at debugging issues yourself, using AI when you get particularly stuck can help you save hours and sometimes days when you encounter particularly tough bugs.
Here are some ways to use AI in debugging. If the AI generates code, the point is to use that to help forward your understanding, not to copy and use the code as is.
explain error messages and stack traces
analyze logs or event data to identify potential causes of crashes
identify security vulnerabilities or performance issues
make suggestions on how to refactor code for readability/maintainability
Example prompts
I’m getting an ‘UnhandledPromiseRejectionWarning’ error in my JavaScript code. Can you explain what this means and how I should handle it?
My app crashes after a few hours of running, and the logs show repeated database connection errors. Can you help me analyze the logs and suggest why the database connections might be failing?
Here’s some Node.js code that interacts with a SQL database using raw queries. Can you check for any SQL injection vulnerabilities?
I have a long function that does multiple things. Can you suggest how to break it down into smaller, more focused functions for better maintainability?
Create a simple application for integrating - in addition to research, you'll also be building small apps to integrate with existing solutions to better understand their functionality and/or demo to your team. You can use generative AI to help set up these simple applications so you can focus on how existing solutions or products work.
Here are some ways you can use AI to help stand up simple applications. The point here is not to use this code for your Capstone project, but rather to help you further your research and understanding of existing solutions and the problems they're solving.
Create a basic Express API
Stand up a simple frontend in React
Set up a CRUD application with Mongo
Build a simple pub/sub system
Create a basic websocket server
Example prompts
Can you generate a basic Express.js API with a couple of endpoints to manage a simple list of tasks (create, read, delete)?
Can you create a basic React app that displays a list of items and allows me to add new ones, with state management?
Can you build a simple Node.js app with a MongoDB database to manage users (create, read, update, delete)?
Can you create a simple pub/sub system in JavaScript using a basic event emitter model?
Can you create a simple WebSocket server in Node.js that broadcasts messages to all connected clients?
Validating understanding and approach - you can use generative AI to validate your understanding of concepts or approaches you are going to take. They can help surface unknowns, and ensure that your understanding of a concept or your plan for solving a problem is complete (or at least well-informed). By describing your understanding or the steps you intend to take, and then asking, “What am I missing?” AI can help point out potential gaps, edge cases, or alternative approaches that you may have overlooked.
Here are some ways you can use AI to validate your understanding and approach.
Confirm understanding
Validate implementation plans
Refining system design
Anticipating edge cases or hidden issues
Example prompts
I’m planning to use optimistic locking to handle concurrent updates in my database. My understanding is that each record has a version number, and updates are only applied if the version number matches. What am I missing in terms of handling conflicts and potential performance issues?
I’m going to implement JWT-based authentication for my API. Users will receive a token upon login, and they’ll include it in the Authorization header for subsequent requests. Is this the correct approach, and am I missing anything about token expiration or security?
I’m designing a microservices architecture where each service communicates via REST APIs. I’m using a single database for all services. What am I missing in terms of service decoupling and performance?
I’m building a form validation system in React that checks for required fields, email format, and password strength. Am I missing any potential edge cases I should account for?
And of course, make sure to validate information from AI - as we know, AI can hallucinate at times, confidently giving you information that is simply incorrect. Make sure to spot check and validate the response from AI, especially if you suspect something is off.
Anti-Patterns

We've given you examples of good ways to use AI during Capstone, and especially with your Capstone project, as a way to help support your understanding and keep you moving forward in a responsible way. There are also ways to use AI that will hinder you, rather than help you, during your Capstone journey.

Using AI to code in "full self driving" mode - the point of the Capstone project is to significantly deepen your understanding of a particular problem space through solving problems with code. Relying on AI to self-drive the entire way might help you move forward more quickly, but ultimately undermines the main goal of learning. Remember, you'll have to talk about and explain your project code during interviews. Use AI to help clarify concepts or give you examples, but write the actual code yourself so you know exactly what it is you're doing.
Using AI that results in feature sprawl- if you relied on AI to do a lot of the heavy lifting during the implementation phase, yes, you can get more done. But more isn't always better, especially with the Capstone project. We've often seen projects with smaller scopes but tightly defined use cases do better on the job hunt than larger projects with more "sprawling" scope and a grab bag of assorted features. For the job hunt, it's always better to have a smaller, more constrained project with feature that all support a specific use case, a project that you know inside and out vs a larger, more ambitious project that you're mildly familiar with.
Overusing AI during the design phase - AI can help point you in the right directions in your project's design, but don't use it to actually make your design or architecture decisions for you. Validate AI's suggestions via discussions with the teams, and make sure to understand multiple options instead of just the option it's giving you.
Generating solutions to hard technical problems - Use AI to explain concepts, stepping through a problem step by step with you, or having it ask you questions to validate your understanding about a problem rather than just turning to it to generate a solution. The former will help you understand the problem in detail, the latter will just get you over the hurdle and wastes the experience as a chance for you to learn.

The above are guidelines and not laws, you must exercise judgement in applying these guidelines. Ultimately, we are not interested in selling a product to end users. YOU are the star of this project and therefore, you must understand what is happening. Given how fast things are changing, it's difficult for us to say for certain you should or should not use AI in this or that way, but keep this underlying thought: use AI and explore and push boundaries, but make sure you remain the centerpiece.
