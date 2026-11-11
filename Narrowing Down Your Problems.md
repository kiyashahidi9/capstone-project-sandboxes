Most Capstone groups think that they need to immediately start looking for project ideas once they narrow down to one problem domain. However, that's not the approach we advise. It's not possible to "look for project ideas" yet. Doing so results in many unanswerable questions and causes more confusion.

Once we've committed to a high-level problem domain, we should instead take the following steps to drill down to a specific problem to tackle. We'll give you the steps to this process and then follow up with an example and explanation of each step:

Within a problem domain, take inventory of existing solutions
Categorize existing solutions into sub-problem domains
Iterate through each sub-problem domain, applying steps 1 & 2
Find and study a model product/project
Come up with a use case
clone vs wrap (if model is open source, otherwise can only clone)
Justify the architecture

Ok, that probably didn't make too much sense by itself. We'll break it down by taking a look at an example from an actual Capstone team in moving from a broad problem domain to a specific use case, followed by an explanation of each step.

Introduction

The Monsoon Capstone team explored various problem domains and decided they want to do a project in the load testing space. They are interested in how applications are tested under load to see how they can scale as the number of users and concurrent requests increase.

1.  Within a problem domain, take inventory of existing solutions

The Monsoon team did some research and took stock of existing solutions within the load testing problem domain. Of the solutions that exist for load testing, they focused on JMeter, Selenium, and Flood.

2.  Categorize existing solutions into sub-problem domains

The above solutions fall under different sub-problem domains referred to as protocol-based load testing (JMeter) and browser-based load testing (Selenium and Flood).

Various existing solutions within a problem domain often solve different sub-problems within that larger domain. Categorize existing solutions into these sub-problem domains, like the Monsoon team did above. One way to do this categorization is to create a technical tradeoff table of different existing solutions (see below). Choose some attributes by which you can categorize all the products in this space, listing the pros and cons of each solution and thinking about what use case they are optimized for. You can also make a chart (line, x/y, or radar). Try to find experts to come up with those attributes, if possible. This is hard and takes a fair amount of research, so don't get discouraged!

Zoom Narrowing Process.drawio.png
Technical Tradeoff

3. Iterate through each sub-problem domain, applying steps 1 & 2

The team is interested in browser-based load testing because they want to load test single page applications (something a protocol-based solution isn't a good fit for), so they look deeper into the browser-based load testing sub-problem. They further sub-categorized browser-based load testing into cloud (Flood) and non-cloud (Selenium) options.

What the Monsoon team did was iterate through each sub-problem domain and recursively apply step 1 & 2 to each sub-problem domain (think, for-loop with a recursive function call in each iteration). The number of times you will need to do this depends on the scope of your problem domain. Make sure to take notes and create a mental map of the larger problem domain, solutions, sub-problem domains, etc. as you drill down. When you talk to each other, refer to this mental map so everyone is aligned.

4. Find and study a model product/project

The Monsoon team decides that Flood is a good model product for them to study. The team creates an account and does some tinkering. They figure out how to set up and configure an actual load test, learn what reasonable default parameters for load testing are, and further understand the pros and cons of Flood's product.

As you go through each sub-problem domain (or sub-sub-sub-problem domain) you'll need to find and study a model product/project as an existing solution. This can be a proprietary product (like Flood in this example) or an open source project. Having a model project to poke around and analyze will help answer a lot of specific and use-case related questions. When exploring a problem domain through research, it can be difficult to visualize the concepts you're studying in a concrete and tangible way. Playing around with model projects often fills in the gaps.

5.  Come up with a use case

The Monsoon team creates a use case where they will build a more narrowly-defined open-source version of Flood that can load test via horizontally scaled compute instances, aggregate metrics from the test, and display it for the user in real time. Users of their project would include teams who want the power and flexibility of a browser-based load testing tool on the cloud, but don't need the entire feature set or price of a full-fledged SaaS product nor do they want to build something themselves.

After steps 1-4, you should have far greater context for the sub-problem domain. It's now time to come up with a use case. This is your solution and what you will build. The use case can often be detected through studying an existing solution (step 4). The existing solution gives confidence about the validity of the use case, otherwise the solution wouldn't exist. There are two major paths: a) build a smaller, more narrowly defined model project (like Flood, for example) or b) wrap the model project into some automated workflow. Note that b) is only possible if your model project is open source. An example of a) would be Mothership, who built a more narrowly defined PaaS. An example of b) would be Pilot, who wrapped the open source project Waypoint and automated some workflows.

6.  Justify the architecture

The Monsoon team used the use case to justify various pieces of their architecture. For example, they used AWS Fargate and ECS in order to horizontally scale out the applications that would be performing the actual load tests based on the user's configuration. They also were able to include a serverless component in their architecture in order to aggregate new test metrics over time.

Find a way to justify the architecture for your use case. Sometimes, this may involve digging into an open source project's ecosystem and community (joining Slack channels, talking with developers, etc). Other times, it's reading and researching white papers and videos to get a hint as to how the model is built.
