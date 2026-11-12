A use case describes the set of circumstances where a given tool or application is optimal, given the tradeoffs. Every project makes design decisions and tradeoffs that make the application suited for some people and not others.

As stated in Finding a Use Case, a project's use case answers a few critical questions:

What are the core engineering challenges that drive this project?
Who experiences this problem? What are the characteristics of the application or development team? What are their requirements?
What set of tradeoffs define who this project is not for?
Where does this project sit in relation to alternative solutions? For whom (or for what types of applications) would it be an optimal approach?
With that in mind, let's examine how a series of Capstone projects in a similar engineering domain demonstrated distinct use cases.

Feature flag platforms
Feature flag platforms allow developers to toggle the functionality of their applications in production without deploying new code.

We've had a number of Capstone projects in this space, and they all provide this basic functionality. Let's take a deeper look at them to understand how each targets a separate use case.

1. Fana
   Zoom image.png
   Fana's tradeoffs

The Fana team observed in their research that sometimes developers want to toggle functionality not for all of their users, but for specific subsets. Usually, those subsets - or audiences - can be defined by the attributes of the users. For example, users in a particular location, or users that have provided an age under 35.

Fana is a feature flag platform built specifically for teams that desire this type of audience targeting with their flags.

2. Tailslide

Zoom image.png
Tailslide's tradeoffs
One of the benefits of feature flags is that they enable developers to "turn off" features in production without a deployment. This can be particularly helpful if a new feature introduces bugs to the application.

This observation is the basis for Tailslide. In addition to a feature flag platform, Tailslide implements a "circuit breaker" algorithm that can automatically turn off a feature wrapped by a feature flag if the feature throws a user-defined number of errors in a given amount of time.

3. Waypost

Example of the results of an experiment in Waypost

Developer teams often want to understand the impact of features that they are planning to launch. They can use feature flags to render different 'versions' of their application and record the behavior of users in each version, a technique called A/B Testing.

Evaluation of the results requires careful statistical analysis. Waypost is a feature flag platform that handles the creation and evaluation of tests for users in different feature flag conditions.

Use cases of feature flag platforms
To summarize:

Fana's use case is audience segmentation and targeting. It may be a great choice for developers who have many different (and potentially overlapping) audiences to target with changes. However, if robust statistical evaluation of behavior is required, then Fana might not be the right choice out of the box
Tailslide's use case is teams that want to safely deploy, rollback, and recover from errors without new deployments. Audience segmentation is not a feature, but teams that want a flag-based option to improve the stability of their applications would be ideal users.
Waypost's use case is A/B testing. It is less suited, out of the box, for circuit breaking, and while it has audience segmentation options, they are designed with testing and evaluation in mind.
Three feature flag platforms, three sets of design decisions, strengths, and tradeoffs!

What about the paid service that does it all?
In the case of feature flags, that service is Launch Darkly, a comprehensive feature flag provider. When you read these case studies, you'll notice it in their discussions of existing solutions.

Teams often wonder how to frame their use case as distinct when there are existing enterprise solutions that seem to cover every use case.

If your project falls into this category, then think about it like this:

Specificity is a benefit. Breadth is a tradeoff.

Many established solutions - particularly paid solutions - cover the majority of use cases because they want to pull in the broadest set of users. What they trade off on is often complexity. Applications that do "everything" often have a higher barrier for entry for newcomers who want to do "one thing".

Think of a tool like Photoshop - it's a comprehensive tool for every graphic design need, but it's challenging for anyone who just wants to quickly crop a photo or add some text.

Use the focus on your distinct use case as an advantage. Understand the needs and requirements of that use case deeply, and orient your design decisions around it. Make it very simple to perform whatever its main use case is -- so simple that newcomers might prefer your project to the comprehensive one.
